#!/usr/bin/env python3
"""RPC Multiplexing Proxy — Standalone MVP.

Multiplexes N llama-server TCP connections over a single RPC server connection.
Each llama-server connects to its own proxy port; all commands serialize on one
shared backend RPC socket. Responses route back to the originating client.

Usage (CLI):
    python3 rpc_proxy.py --rpc-endpoint dllama1.lan:50052 \\
                         --base-port 50052 --max-connections 4 -v

Usage (env vars, fallback when CLI opts missing):
    RPC_PROXY_RPC_ENDPOINT=dllama1.lan:50052 \\
    RPC_PROXY_BASE_PORT=50052 \\
    RPC_PROXY_MAX_CONNECTIONS=4 \\
    python3 rpc_proxy.py -v

Phase 1 (core): multi-port listener, FIFO serialization, pass-through.
Phase 2 (shared buffers): tensor name tracking, first-wins allocation.
"""

import argparse
import hashlib
import logging
import os
import signal
import socket
import struct
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import Optional


# ── Protocol Constants ────────────────────────────────────────────────

# RPC command types (uint8 — from ggml-rpc.cpp enum rpc_cmd)
CMD_ALLOC_BUFFER    = 0
CMD_GET_ALIGNMENT   = 1
CMD_GET_MAX_SIZE    = 2
CMD_BUFFER_GET_BASE = 3
CMD_FREE_BUFFER     = 4
CMD_BUFFER_CLEAR    = 5
CMD_SET_TENSOR      = 6
CMD_SET_TENSOR_HASH = 7
CMD_GET_TENSOR      = 8
CMD_COPY_TENSOR     = 9
CMD_GRAPH_COMPUTE   = 10
CMD_GET_DEVICE_MEMORY = 11
CMD_INIT_TENSOR     = 12
CMD_GET_ALLOC_SIZE  = 13
CMD_HELLO           = 14
CMD_DEVICE_COUNT    = 15
CMD_GRAPH_RECOMPUTE = 16

CMD_NAMES = {
    CMD_ALLOC_BUFFER: "ALLOC_BUFFER",
    CMD_GET_ALIGNMENT: "GET_ALIGNMENT",
    CMD_GET_MAX_SIZE: "GET_MAX_SIZE",
    CMD_BUFFER_GET_BASE: "BUFFER_GET_BASE",
    CMD_FREE_BUFFER: "FREE_BUFFER",
    CMD_BUFFER_CLEAR: "BUFFER_CLEAR",
    CMD_SET_TENSOR: "SET_TENSOR",
    CMD_SET_TENSOR_HASH: "SET_TENSOR_HASH",
    CMD_GET_TENSOR: "GET_TENSOR",
    CMD_COPY_TENSOR: "COPY_TENSOR",
    CMD_GRAPH_COMPUTE: "GRAPH_COMPUTE",
    CMD_GET_DEVICE_MEMORY: "GET_DEVICE_MEMORY",
    CMD_INIT_TENSOR: "INIT_TENSOR",
    CMD_GET_ALLOC_SIZE: "GET_ALLOC_SIZE",
    CMD_HELLO: "HELLO",
    CMD_DEVICE_COUNT: "DEVICE_COUNT",
    CMD_GRAPH_RECOMPUTE: "GRAPH_RECOMPUTE",
}

# Commands that produce a response from the RPC server.
# Based on ggml-rpc.cpp: all commands except SET_TENSOR (6) and GRAPH_COMPUTE (10)
# send a response via send_msg() in the server's main loop.
RESPONDED_COMMANDS = {
    CMD_ALLOC_BUFFER,    # 0
    CMD_GET_ALIGNMENT,   # 1
    CMD_GET_MAX_SIZE,    # 2
    CMD_BUFFER_GET_BASE, # 3
    CMD_FREE_BUFFER,     # 4 (sends size=0)
    CMD_BUFFER_CLEAR,    # 5 (sends size=0)
    CMD_SET_TENSOR_HASH, # 7
    CMD_GET_TENSOR,      # 8
    CMD_COPY_TENSOR,     # 9
    CMD_GET_DEVICE_MEMORY, # 11
    CMD_INIT_TENSOR,     # 12 (sends size=0)
    CMD_GET_ALLOC_SIZE,  # 13
    CMD_DEVICE_COUNT,    # 15
    CMD_GRAPH_RECOMPUTE, # 16
}

# Note: The RPC server's main loop returns immediately on cmd=14 (HELLO),
# closing the connection. After initial handshake, proxy skips forwarding
# HELLO to RPC server and replays cached response locally.

# Size of the binary header prefix per RPC message.
# Request:  [cmd:1][size:8][data:size]   → 9 bytes before data
# Response: [size:8][data:size]          → 8 bytes before data
HEADER_SIZE_REQ = 9
HEADER_SIZE_RESP = 8

# HELLO handshake payload sizes (from ggml-rpc.cpp transport.h)
# conn_caps field in rpc_msg_hello_req and rpc_msg_hello_rsp
RPC_CONN_CAPS_SIZE = 24
HELLO_REQ_SIZE = RPC_CONN_CAPS_SIZE          # 24 bytes
HELLO_RSP_SIZE = 4 + RPC_CONN_CAPS_SIZE      # major(1) + minor(1) + patch(1) + padding(1) + conn_caps(24) = 28

# Buffer for reading from sockets.
READ_BUF_SIZE = 256 * 1024  # 256 KB — enough for any single RPC message


# ── Logging Setup ─────────────────────────────────────────────────────

_LOG_FILE = "/tmp/rpc_proxy.log"
_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 1


def _setup_logging(verbose: bool) -> logging.Logger:
    """Configure console (if verbose) + rotating file handler."""
    logger = logging.getLogger("rpc_proxy")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # File handler — always active, INFO level minimum
    fh = RotatingFileHandler(
        _LOG_FILE, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT,
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(fh)

    # Console handler — verbose only
    if verbose:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
        logger.addHandler(ch)

    return logger


# ── IPv6-Compatible Socket Helpers ────────────────────────────────────

def _create_server_socket(host: str, port: int) -> socket.socket:
    """Create a dual-stack IPv4/IPv6 TCP server socket.

    Binds to 0.0.0.0 for IPv4 or :: for IPv6 depending on address family.
    Sets SO_REUSEADDR to allow quick port reuse after restart.
    """
    # Resolve to determine address family — use first result that works
    families = (socket.AF_INET6, socket.AF_INET)

    for addr_family in families:
        sock = socket.socket(addr_family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        bind_addr = "::" if addr_family == socket.AF_INET6 else "0.0.0.0"
        try:
            sock.bind((bind_addr, port))
            sock.listen(128)
            return sock
        except OSError:
            sock.close()

    # Last resort — fall through to whatever worked or raise
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(128)
    return sock


def _connect_to(endpoint: str, timeout: float = 5.0) -> socket.socket:
    """Connect to a TCP endpoint with IPv4/IPv6 auto-detection.

    Uses getaddrinfo for full dual-stack support. Tries each resolved
    address until one succeeds.
    """
    # Parse host:port
    if ":" in endpoint:
        # Could be IPv6 [::1]:50052 or IPv4 1.2.3.4:50052
        if endpoint.startswith("["):
            bracket_end = endpoint.index("]")
            host = endpoint[1:bracket_end]
            port = int(endpoint[bracket_end + 2:])
        else:
            host, _, port_str = endpoint.rpartition(":")
            port = int(port_str) if port_str else 50052
    else:
        host = endpoint
        port = 50052

    # Resolve with getaddrinfo for IPv4/IPv6 auto-detection
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    last_err = None

    for af, socktype, proto, _canonname, sa in infos:
        sock = socket.socket(af, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sa)
            return sock
        except OSError as e:
            last_err = e
            sock.close()

    raise ConnectionRefusedError(
        f"Could not connect to {endpoint}: {last_err}"
    ) from last_err


# ── Binary Protocol Helpers ───────────────────────────────────────────

def _read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly N bytes from a socket with retry on short reads."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), READ_BUF_SIZE))
        if not chunk:
            raise ConnectionError("Connection closed by remote peer")
        buf.extend(chunk)
    return bytes(buf)


def _read_request(sock: socket.socket) -> tuple[int, bytes]:
    """Read an RPC request: [cmd:1][size:8][data:size].

    Returns (cmd_type, data_bytes).
    """
    header = _read_exact(sock, HEADER_SIZE_REQ)
    cmd = header[0]
    size = int.from_bytes(header[1:9], byteorder="little")
    data = _read_exact(sock, size) if size > 0 else b""
    return cmd, data


def _build_request(cmd: int, data: bytes) -> bytes:
    """Pack an RPC request into [cmd:1][size:8][data:size].

    Note: size is uint64 (Q), NOT uint32 (I). The protocol uses 8-byte size field.
    """
    return struct.pack("<BQ", cmd, len(data)) + data


def _read_response(sock: socket.socket) -> bytes:
    """Read an RPC response: [size:8][data:size].

    Returns the data payload (without the 8-byte size header).
    """
    size_header = _read_exact(sock, HEADER_SIZE_RESP)
    size = int.from_bytes(size_header, byteorder="little")
    if size == 0:
        return b""
    return _read_exact(sock, size)


def _build_response(data: bytes) -> bytes:
    """Pack a response payload with its size header: [size:8][data:size]."""
    return struct.pack("<Q", len(data)) + data


def _parse_hello_response(data: bytes) -> dict:
    """Parse a raw HELLO response payload into version info.

    The payload is a raw struct: [major:1][minor:1][patch:1][padding:1][conn_caps:24].

    Returns dict with version info and capabilities, or empty dict on error.
    """
    try:
        if len(data) < HELLO_RSP_SIZE:
            return {}
        major = data[0]
        minor = data[1]
        patch = data[2]
        # padding = data[3]  # unused
        conn_caps = data[4:4 + RPC_CONN_CAPS_SIZE]
        return {"version": f"{major}.{minor}.{patch}", "conn_caps": conn_caps}
    except (ConnectionError, OSError):
        return {}


# ── Tensor Name Extraction ────────────────────────────────────────────

def _extract_tensor_name_from_set_tensor(data: bytes) -> Optional[str]:
    """Try to extract tensor name from a SET_TENSOR command payload.

    The SET_TENSOR payload in ggml-rpc.cpp contains an `rpc_tensor` struct:
        char name[64];
        uint32_t ne[4];
        uint32_t type;
        ...
    The tensor name is the first 64 bytes (null-terminated C string).

    Returns the tensor name string, or None if extraction fails.
    """
    try:
        if len(data) < 64:
            return None
        raw = data[:64]
        # Find null terminator
        end = raw.find(b"\x00")
        if end == -1:
            name = raw.decode("utf-8", errors="replace").strip()
        else:
            name = raw[:end].decode("utf-8", errors="replace").strip()
        return name if name else None
    except Exception:
        return None


def _compute_fingerprint(tensor_names: list[str]) -> str:
    """Deterministic fingerprint from sorted tensor names.

    Used to verify all clients load the same model before sharing buffers.
    SHA256 of newline-joined, sorted tensor names → hex string (first 16 chars).
    """
    parts = sorted(tensor_names)
    if not parts:
        return ""
    fp_string = "\n".join(parts)
    return hashlib.sha256(fp_string.encode()).hexdigest()[:16]


# ── Shared Buffer Registry ────────────────────────────────────────────

class BufferRegistry:
    """Tracks tensor → remote_ptr mappings across all clients.

    First-wins: the first client to set a tensor establishes the baseline.
    Subsequent clients with the same tensor name share that buffer.

    Model fingerprinting verifies all connected clients use the same model.
    """

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        # tensor_name → remote_ptr (first allocation wins)
        self.shared_tensors: dict[str, str] = {}
        # client_id → set of tensor names this client loaded first
        self.client_first_loads: dict[int, set[str]] = {}
        # Baseline fingerprint from first client's first tensor
        self.model_fingerprint: Optional[str] = None
        # Track which client owns each shared tensor (for logging)
        self.tensor_owner: dict[str, int] = {}

    def on_set_tensor(self, client_id: int, tensor_name: str, remote_ptr: str) -> str:
        """Process a SET_TENSOR command. Returns status: 'first_load' | 'shared' | 'mismatch'.

        Phase 2 shared buffer detection:
        - If tensor_name already tracked → mark as shared, return 'shared'
        - If first time → register baseline, check fingerprint, return 'first_load'
        """
        if tensor_name in self.shared_tensors:
            self.logger.debug(
                "SET_TENSOR SHARED tensor=%s ptr=%s (owned by client %d)",
                tensor_name, remote_ptr, self.tensor_owner.get(tensor_name, -1),
            )
            return "shared"

        # First load — register as baseline
        self.shared_tensors[tensor_name] = remote_ptr
        self.tensor_owner[tensor_name] = client_id

        if client_id not in self.client_first_loads:
            self.client_first_loads[client_id] = set()
        self.client_first_loads[client_id].add(tensor_name)

        # Compute and verify model fingerprint
        fp = _compute_fingerprint(
            list(self.client_first_loads.get(0, set())),
        )
        if self.model_fingerprint is None:
            self.model_fingerprint = fp
            self.logger.info(
                "SET_TENSOR first_load tensor=%s ptr=%s (model fingerprint: %s)",
                tensor_name, remote_ptr, fp,
            )
        else:
            # Check if this client's tensors match the baseline
            new_fp = _compute_fingerprint(
                list(self.client_first_loads.get(client_id, set())),
            )
            if new_fp and new_fp != self.model_fingerprint:
                self.logger.warning(
                    "Model fingerprint mismatch: client %d (%s) vs baseline (%s)",
                    client_id, new_fp, self.model_fingerprint,
                )
                return "mismatch"

        self.logger.info(
            "SET_TENSOR first_load tensor=%s ptr=%s (baseline established)",
            tensor_name, remote_ptr,
        )
        return "first_load"

    def on_free_buffer(self, client_id: int) -> None:
        """Clean up a disconnected client's entries from the registry."""
        if client_id in self.client_first_loads:
            removed = self.client_first_loads.pop(client_id)
            self.logger.info(
                "Client %d freed %d tensor(s)", client_id, len(removed),
            )

    def get_status(self) -> dict:
        """Return registry status for health/status reporting."""
        return {
            "shared_tensors": len(self.shared_tensors),
            "active_clients": len(self.client_first_loads),
            "fingerprint": self.model_fingerprint or "N/A",
        }


# ── Client Connection Handler ─────────────────────────────────────────

class ClientSession:
    """Manages the read loop for one connected llama-server client.

    Each client gets its own socket and a dedicated reader thread.
    Commands are forwarded to the proxy's FIFO queue; responses route back
    via the dispatch thread which holds the client socket dict.
    """

    def __init__(
        self,
        client_id: int,
        client_sock: socket.socket,
        command_queue: "deque[tuple[int, int, bytes]]",
        logger: logging.Logger,
    ):
        self.client_id = client_id
        self.sock = client_sock
        self.command_queue = command_queue
        self.logger = logger
        self.alive = True

    def run(self) -> None:
        """Main read loop: receive RPC commands, enqueue for proxy dispatch."""
        self.logger.info("Client %d connected on port %d", self.client_id, self.sock.getsockname()[1])

        try:
            while self.alive:
                cmd, data = _read_request(self.sock)
                cmd_name = CMD_NAMES.get(cmd, f"UNKNOWN({cmd})")
                self.logger.info("Client %d → %s (%d bytes)", self.client_id, cmd_name, len(data))

                # Queue for proxy dispatch (FIFO order)
                self.command_queue.append((self.client_id, cmd, data))

        except ConnectionError:
            self.logger.warning("Client %d disconnected", self.client_id)
        except OSError as e:
            self.logger.error("Client %d socket error: %s", self.client_id, e)
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass


# ── RPC Proxy Core ────────────────────────────────────────────────────

class RpcProxy:
    """Main proxy instance — multiplexes N clients over 1 RPC connection.

    Architecture (single dispatcher pattern):
      Client threads → read from client sockets → queue commands
      Dispatch thread → pop queue → send to RPC → read response → route to client socket
      All responses are handled in-line by the dispatch thread.

    This avoids the multi-thread response routing problem: one thread owns
    the RPC socket, reads responses in order, and writes directly to
    client sockets via a dict lookup.
    """

    def __init__(
        self,
        rpc_endpoint: str,
        base_port: int = 50052,
        max_connections: int = 4,
        verbose: bool = False,
    ):
        self.rpc_endpoint = rpc_endpoint
        self.base_port = base_port
        self.max_connections = max_connections

        self.logger = _setup_logging(verbose)
        self.buffer_registry = BufferRegistry(self.logger)

        # Command queue: (client_id, cmd_type, data)
        self.command_queue: "deque[tuple[int, int, bytes]]" = deque()
        self.queue_lock = threading.Lock()
        self.queue_not_empty = threading.Condition(self.queue_lock)

        # RPC connection state
        self.rpc_conn: Optional[socket.socket] = None
        self.rpc_conn_lock = threading.Lock()

        # Cached HELLO response from initial handshake. Used to replay
        # the version response when clients send HELLO (the server's main
        # loop returns immediately on cmd=14 without sending a response).
        self._cached_hello_response: bytes = b""

        # Listener sockets per port: [(client_id, listener_sock), ...]
        self.listeners: list[tuple[int, socket.socket]] = []
        self.client_threads: list[threading.Thread] = []

        # Client ID → socket map for response routing.
        # Protected by queue_lock (shared with command_queue).
        self.client_sockets: dict[int, socket.socket] = {}

        # Shutdown signaling
        self.shutdown_event = threading.Event()
        self._running = False

    # ── Listener Management ─────────────────────────────────────────

    def _start_listeners(self) -> None:
        """Create and bind all proxy listener sockets."""
        for i in range(self.max_connections):
            port = self.base_port + i
            sock = _create_server_socket("0.0.0.0", port)
            self.listeners.append((i, sock))
            self.logger.info(
                "Listening on port %d (client_id=%d)", port, i,
            )

    def _accept_loop(self, client_id: int, listener_sock: socket.socket) -> None:
        """Accept connections on one listener port."""
        self.logger.info(
            "Accept loop started for client_id=%d on port %d",
            client_id, listener_sock.getsockname()[1],
        )

        while not self.shutdown_event.is_set():
            try:
                listener_sock.settimeout(1.0)
                client_sock, addr = listener_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.shutdown_event.is_set():
                    self.logger.error("Accept error on port %d", listener_sock.getsockname()[1])
                break

            # Verify we're not at capacity
            active_count = sum(1 for t in self.client_threads if t.is_alive())
            if active_count >= self.max_connections:
                self.logger.warning(
                    "At capacity (%d/%d) — rejecting connection from %s",
                    active_count, self.max_connections, addr,
                )
                client_sock.close()
                continue

            # Register socket for response routing
            with self.queue_lock:
                self.client_sockets[client_id] = client_sock

            # Create and start client session (reader thread)
            session = ClientSession(
                client_id=client_id,
                client_sock=client_sock,
                command_queue=self.command_queue,
                logger=self.logger,
            )
            thread = threading.Thread(
                target=session.run, daemon=True,
                name=f"client-{client_id}",
            )
            self.client_threads.append(thread)
            thread.start()

    # ── RPC Connection Management ───────────────────────────────────

    def _connect_rpc(self) -> None:
        """Connect to the backend RPC server and perform HELLO handshake.

        The RPC server requires HELLO as the first command on any new connection.
        This method loops until connected + HELLO successful, or shutdown.
        """
        while not self.shutdown_event.is_set():
            try:
                self.rpc_conn = _connect_to(self.rpc_endpoint)
                self.logger.info("RPC connection established → %s", self.rpc_endpoint)

                # Send HELLO handshake with correct struct size (24 bytes conn_caps)
                hello_data = b"\x00" * RPC_CONN_CAPS_SIZE
                hello = _build_request(CMD_HELLO, hello_data)
                self.rpc_conn.sendall(hello)

                # Read HELLO response — server sends [size:8][data:28]
                self._cached_hello_response = _read_response(self.rpc_conn)
                parsed = _parse_hello_response(self._cached_hello_response)
                self.logger.info(
                    "RPC HELLO handshake complete — version %s (conn_caps=%d bytes)",
                    parsed.get("version", "?"), len(parsed.get("conn_caps", b"")),
                )
                return

            except (ConnectionRefusedError, OSError) as e:
                # Close stale socket before retry
                try:
                    if self.rpc_conn:
                        self.rpc_conn.close()
                except OSError:
                    pass
                self.logger.warning("RPC connect failed: %s — retrying in 2s", e)
                time.sleep(2)

    # ── Single Dispatch Loop (send + receive + route) ───────────────

    def _dispatch_loop(self) -> None:
        """Single-threaded dispatch loop.

        Owns the RPC connection socket. Reads commands from the queue,
        sends them to RPC, reads responses, and routes them back to clients.

        Because the RPC protocol is strictly synchronous (one request at a time),
        all I/O on the RPC side can happen in one thread. Client reads are
        handled by separate reader threads that only enqueue commands.
        """
        self.logger.info("Dispatch loop started")

        while not self.shutdown_event.is_set():
            # Wait for a command in the queue
            with self.queue_lock:
                while not self.command_queue and not self.shutdown_event.is_set():
                    self.queue_not_empty.wait(timeout=0.5)

                if self.shutdown_event.is_set() and not self.command_queue:
                    break

                if not self.command_queue:
                    continue

                client_id, cmd_type, data = self.command_queue.popleft()

            # Send to RPC server (acquire lock since only dispatch thread writes)
            try:
                with self.rpc_conn_lock:
                    if self.rpc_conn is None:
                        # Re-queue and wait for reconnection
                        with self.queue_lock:
                            self.command_queue.appendleft((client_id, cmd_type, data))
                            self.queue_not_empty.notify()
                        time.sleep(0.5)
                        continue

                    # Skip forwarding HELLO to RPC server — it's already been
                    # handled by the initial handshake in _connect_rpc().
                    # The server's main loop returns on cmd=14, closing the
                    # connection if we forward another HELLO.
                    if cmd_type == CMD_HELLO:
                        self.logger.info(
                            "Forwarding client %d HELLO — skipping RPC send (already handshaked)",
                            client_id,
                        )
                        # Route cached response back to client
                        self._route_response(client_id, cmd_type, data,
                                             self._cached_hello_response)
                        continue

                    req = _build_request(cmd_type, data)
                    self.rpc_conn.sendall(req)

                # If this command expects a response, read and route it
                if cmd_type in RESPONDED_COMMANDS:
                    try:
                        # For HELLO responses, the RPC server sends [size:8][data:28] format.
                        # _read_response handles this correctly by reading the 8-byte size
                        # prefix then the data payload.
                        self.logger.info(
                            "Sending to RPC: client %d cmd=%s (%d bytes)",
                            client_id, CMD_NAMES.get(cmd_type, str(cmd_type)), len(data),
                        )
                        resp_data = _read_response(self.rpc_conn)
                        self.logger.info(
                            "Response received for client %d cmd=%s (%d bytes)",
                            client_id, CMD_NAMES.get(cmd_type, str(cmd_type)), len(resp_data),
                        )
                        self._route_response(client_id, cmd_type, data, resp_data)
                    except (ConnectionError, OSError) as e:
                        self.logger.error(
                            "Response read failed for client %d cmd=%d: %s",
                            client_id, cmd_type, e,
                        )
                else:
                    # GRAPH_COMPUTE is fire-and-forget — no response to read
                    self.logger.debug(
                        "Sent (no-response) client %d cmd=%d (%d bytes)",
                        client_id, cmd_type, len(data),
                    )

            except (ConnectionError, OSError) as e:
                self.logger.error("Send to RPC failed: %s", e)
                # Re-queue for retry after reconnection
                with self.queue_lock:
                    self.command_queue.appendleft((client_id, cmd_type, data))
                    self.queue_not_empty.notify()

        self.logger.info("Dispatch loop stopped")

    def _route_response(
        self, client_id: int, cmd_type: int, original_data: bytes, resp_data: bytes,
    ) -> None:
        """Route an RPC response back to the originating client.

        Handles Phase 2 shared buffer detection for SET_TENSOR commands.
        """
        # Phase 2: shared buffer detection for SET_TENSOR
        if cmd_type == CMD_SET_TENSOR:
            tensor_name = _extract_tensor_name_from_set_tensor(original_data)
            if tensor_name:
                status = self.buffer_registry.on_set_tensor(
                    client_id, tensor_name, f"resp_{len(resp_data)}",
                )
                if status == "shared":
                    self.logger.info(
                        "Response routed: SHARED buffer for client %d (tensor=%s)",
                        client_id, tensor_name,
                    )

        # Build and send response back to the client socket
        try:
            response_bytes = _build_response(resp_data)
        except Exception as exc:
            self.logger.error(
                "Failed to build response for client %d cmd=%d: %s",
                client_id, cmd_type, exc,
            )
            return

        with self.queue_lock:
            client_sock = self.client_sockets.get(client_id)

        if client_sock is None:
            self.logger.warning(
                "Response for client %d: socket not found", client_id,
            )
            return

        try:
            client_sock.sendall(response_bytes)
        except OSError as e:
            self.logger.error("Failed to send response to client %d: %s", client_id, e)

    # ── Lifecycle Management ────────────────────────────────────────

    def start(self) -> None:
        """Start the proxy: listeners, RPC connection, dispatch loop."""
        self._running = True

        # 1. Start acceptor threads (one per port)
        self._start_listeners()
        for client_id, listener_sock in self.listeners:
            t = threading.Thread(
                target=self._accept_loop, args=(client_id, listener_sock),
                daemon=True, name=f"accept-{client_id}",
            )
            t.start()

        self.logger.info(
            "Proxy listening on ports %d-%d (max=%d) -> %s",
            self.base_port, self.base_port + self.max_connections - 1,
            self.max_connections, self.rpc_endpoint,
        )

        # 2. Connect to RPC server
        self._connect_rpc()

        # 3. Start dispatch loop (owns RPC connection — send + receive + route)
        dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="dispatcher",
        )
        dispatch_thread.start()

        self.logger.info("RPC proxy started - all components active")

    def stop(self) -> None:
        """Graceful shutdown: drain queue, close connections."""
        self.logger.info("Shutting down...")
        self.shutdown_event.set()
        self._running = False

        # Wake up the dispatcher and accept loops
        with self.queue_lock:
            self.queue_not_empty.notify_all()

        # Wait for client reader threads to finish (with timeout)
        for t in self.client_threads:
            t.join(timeout=3)

        # Close all tracked client sockets
        with self.queue_lock:
            for sock in self.client_sockets.values():
                try:
                    sock.close()
                except OSError:
                    pass
            self.client_sockets.clear()

        # Close listener sockets
        for _, sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass

        # Close RPC connection
        with self.rpc_conn_lock:
            try:
                if self.rpc_conn:
                    self.rpc_conn.close()
            except OSError:
                pass

        self.logger.info("Proxy stopped")


# ── CLI Entry Point ───────────────────────────────────────────────────

def _get_config_from_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read an environment variable for proxy configuration.

    Fallback hierarchy: CLI arg > env var > default.
    Env vars use prefix RPC_PROXY_ (e.g., RPC_PROXY_RPC_ENDPOINT).
    """
    return os.environ.get(key, default)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments with env var fallback."""
    parser = argparse.ArgumentParser(
        description="RPC Multiplexing Proxy — multiplexes N llama-servers over 1 RPC connection.",
    )
    parser.add_argument(
        "--rpc-endpoint",
        default=_get_config_from_env("RPC_PROXY_RPC_ENDPOINT"),
        help="Backend RPC server address (host:port). Env: RPC_PROXY_RPC_ENDPOINT",
    )
    parser.add_argument(
        "--base-port",
        type=int,
        default=int(_get_config_from_env("RPC_PROXY_BASE_PORT", "50052")),
        help="First port in the proxy listen range. Env: RPC_PROXY_BASE_PORT (default 50052)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=int(_get_config_from_env("RPC_PROXY_MAX_CONNECTIONS", "4")),
        help="Max simultaneous client connections. Env: RPC_PROXY_MAX_CONNECTIONS (default 4)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=_get_config_from_env("RPC_PROXY_VERBOSE", "false").lower() == "true",
        help="Enable verbose console logging. Env: RPC_PROXY_VERBOSE=true",
    )

    args = parser.parse_args()

    # Validate — exit if essential config missing
    if not args.rpc_endpoint:
        parser.error(
            "--rpc-endpoint is required (or set RPC_PROXY_RPC_ENDPOINT env var). "
            "Example: --rpc-endpoint dllama1.lan:50052"
        )

    return args


def main() -> None:
    """Entry point: parse config, create proxy, start, wait for shutdown."""
    args = parse_args()

    # Print configuration summary
    print(f"[rpc_proxy] Starting — rpc_endpoint={args.rpc_endpoint} "
          f"base_port={args.base_port} max_connections={args.max_connections} verbose={args.verbose}")
    print(f"[rpc_proxy] Listening on ports {args.base_port}-{args.base_port + args.max_connections - 1}")

    proxy = RpcProxy(
        rpc_endpoint=args.rpc_endpoint,
        base_port=args.base_port,
        max_connections=args.max_connections,
        verbose=args.verbose,
    )

    # Register signal handlers for graceful shutdown
    def _handle_signal(signum, frame):
        proxy.logger.info("Signal %d received — initiating shutdown", signum)
        proxy.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        proxy.start()
        # Block until shutdown
        while proxy._running:
            proxy.shutdown_event.wait(timeout=2.0)
    except KeyboardInterrupt:
        proxy.stop()


if __name__ == "__main__":
    main()
