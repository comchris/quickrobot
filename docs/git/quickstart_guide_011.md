# Quickrobot — Quickstart Guide

**Version:** v0.11 | **Last updated:** 2026-07-30

---

## Table of Contents

1. [Overview](#1-overview)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [First Look at the WebUI](#4-first-look-at-the-webui)
5. [Add Your First Remote Node](#5-add-your-first-remote-node)
6. [Scan for Models](#6-scan-for-models)
7. [Create a Preset](#7-create-a-preset)
8. [Deploy Your First llama.cpp Server](#8-deploy-your-first-llamacpp-server)
9. [What Gets Deployed](#9-what-gets-deployed)
10. [Deploy an RPC Instance on an Additional Node](#10-deploy-an-rpc-instance-on-an-additional-node)
11. [Set Up a Minimal Cluster](#11-set-up-a-minimal-cluster)
12. [Debug and Monitor](#12-debug-and-monitor)

---

## 1. Overview {#1-overview}

quickrobot lets you manage LLM inference servers from a browser. You add remote machines as "nodes," place GGUF model files on them, create presets (named configurations), and deploy llama.cpp servers with one click.

**What you need:**
- One API host machine (where quickrobot runs)
- Optional: additional remote nodes with GPUs or CPUs
- SSH access from the API host to each remote node
- Passwordless sudo on each remote node

---

## 2. Installation {#2-installation}

### 2a — API host (Debian packages, no pip)

Install all quickrobot dependencies from Debian repos:

```bash
sudo apt update
sudo apt install -y python3 python3-pip git sshpass \
    ansible-core python3-flask python3-jinja2 python3-markupsafe python3-yaml \
    python3-psutil python3-requests python3-requests-ntlm \
    python3-flask-cors python3-flask-socketio python3-waitress \
    libssl-dev libffi-dev pkg-config curl jq build-essential
```

After this, clone and run:

```bash
cd /opt
sudo mkdir -p quickrobot && sudo chown $USER:$USER quickrobot
cd quickrobot
git clone https://github.com/comchris/quickrobot.git .
python3 quickrobot.py
```

No virtual environment needed. All Python packages come from Debian's `python3-*` packages. This is the simplest path for a single-host setup.

### 2b — API host (pip + venv)

Use a virtual environment to isolate quickrobot's Python dependencies:

```bash
cd /opt
sudo mkdir -p quickrobot && sudo chown $USER:$USER quickrobot
cd quickrobot

git clone https://github.com/comchris/quickrobot.git .
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
python3 quickrobot.py
```

> **Note:** Choose 2a OR 2b — not both. The apt path (2a) installs everything from Debian packages; the venv path (2b) uses pip to isolate Python dependencies. The venv approach is recommended for production or when other Python projects on the same machine need different versions.

### Remote nodes

On each remote machine that quickrobot will manage, install only what Ansible needs:

```bash
sudo apt update
sudo apt install -y python3 sshpass sudo
pip3 install --user ansible-core
```

Remote nodes do not need quickrobot's pip packages, venv, or the WebUI — they only run Ansible playbooks triggered by the API host. Binary templates (pre-built downloads) need nothing extra. Only git builds require `cmake` and build tools on the remote node:

```bash
sudo apt install -y cmake build-essential libvulkan1
```

### Passwordless sudo

quickrobot needs root access on remote nodes for systemd operations. Configure passwordless sudo:

```bash
# Option A: for your existing user (e.g., remoteuser)
echo 'remoteuser ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/remoteuser
sudo chmod 0440 /etc/sudoers.d/remoteuser

# Option B: create a dedicated quickrobot user
sudo adduser --disabled-password --gecos "" quickrobot
sudo usermod -aG sudo quickrobot
echo 'quickrobot ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/quickrobot
sudo chmod 0440 /etc/sudoers.d/quickrobot
```

Verify: `ssh remoteuser@remote-node "sudo -n true && echo OK"` — should print `OK` without asking for a password.

---

## 3. Configuration {#3-configuration}

Copy the sample environment file and adjust:

```bash
cp .quickrobot.env.sample .quickrobot.env
nano .quickrobot.env
```

### Settings to review (minimum)

| Setting | What it does |
|---------|-------------|
| `QUICKROBOT_API_HOST` | Address the API listens on (`127.0.0.1` for local only, or your LAN IP) |
| `QUICKROBOT_API_PORT` | API port (default: 8039) |
| `QUICKROBOT_WEBUI_PORT` | WebUI port (default: 8038) |
| `QUICKROBOT_MCP_HOST` | MCP bind address (change to your LAN IP if an LLM client connects remotely) |
| `QUICKROBOT_MCP_PORT` | MCP port (default: 8040) |
| `QUICKROBOT_WEBUI_PASSWORD` | Set this for the WebUI login page |
| `QUICKROBOT_API_KEY` | API authentication key (auto-generated on first startup) |
| `QUICKROBOT_MCP_TOKEN` | MCP SSE token (auto-generated on first startup) |
| `QUICKROBOT_API_MODEL_BASE_PATH` | **Global default model path** — where GGUF files live on remote nodes (default: `/opt/quickrobot/models`) |

### Key setting: Model base path

`QUICKROBOT_API_MODEL_BASE_PATH` tells the model scanner where to look for `.gguf` files on remote nodes. If your models are in a different location, change it here:

```
QUICKROBOT_API_MODEL_BASE_PATH=/mnt/llama/gguf/models
```

You can also override this per-node later from the WebUI.

### SSH credentials (optional)

If all remote nodes use the same SSH user, set these in `.env` to avoid entering them per-host:

```
QUICKROBOT_API_ANSIBLE_SSHUSER=remoteuser
QUICKROBOT_API_ANSIBLE_SSHKEY=/home/remoteuser/.ssh/id_ed25519
```

### Engine defaults (for git builds)

When using git builds (not binary templates), the following engine defaults control how quickrobot compiles llama.cpp on remote nodes:

| Setting | Default | When to change |
|---------|---------|----------------|
| `binary_path` (llama_server / llama_rpc) | `/opt/quickrobot/llama.cpp/build/bin/llama-server` | If you use a different build output path |
| `git_clone_url` | `https://github.com/ggerganov/llama.cpp.git` | Use a forked repo or specific branch |
| `model_root_path` | `/mnt/llama/gguf/models` | Match your model storage location |
| `node_build_run_cmd` | `cmake --build build --config Release -j$(nproc)` | Reduce `-j` value on machines with limited cores |

> **Tip:** These settings are shared across all instances of a given engine type. If you point `git_clone_url` to a fork, every llama_server on every node will use that fork. Override per-node via the WebUI's node settings if you need different repos per host.

### Start the API

```bash
python3 quickrobot.py
```

On first startup with no database, quickrobot will:
1. Validate the seed file (checksum + size check)
2. Create the SQLite database and import seed data
3. Auto-start the WebUI, MCP server, and scheduler in the background

### Log in to the WebUI

Open your browser at `http://127.0.0.1:8038/webui/login`. Enter the password you set in `QUICKROBOT_WEBUI_PASSWORD`.

---

## 4. First Look at the WebUI {#4-first-look-at-the-webui}

The WebUI has these main sections (accessible from the left sidebar):

- **Instances** — list of all deployed servers (running, stopped, deploying, error)
- **Nodes** — registered remote machines and their health status
- **Models** — scanned model files with path, quantization, and size info
- **Presets** — reusable configuration templates (model + CLI args + env vars)
- **Herd Cluster** — view and manage RPC bindings across server instances
- **Engine Binaries** — pre-built binary templates you can use for fast deploys

On first login you will see:
- 4 system-managed instances (API, WebUI, MCP, Scheduler) marked with a shield icon
- No user instances yet
- No nodes registered yet

---

## 5. Add Your First Remote Node {#5-add-your-first-remote-node}

### Prerequisites before adding a node

Ensure the remote machine has:
- Python 3 installed (verified via `ssh user@host "python3 --version"`)
- Ansible available in PATH (verified via `ssh user@host "ansible --version"`)
- Passwordless sudo (verified via `ssh user@host "sudo -n true && echo OK"`)
- An SSH key from the API host copied to the remote node's `~/.ssh/authorized_keys`

### Add the node via WebUI

1. Navigate to **Nodes** in the left sidebar
2. Click **Add Node** (top-right button)
3. Fill in the form:
   - **Name:** A friendly name like `dllama1`
   - **Hostname:** The machine's DNS name (e.g., `dllama1.lan`) or IP address
   - **SSH User:** Your SSH username on the remote machine (or leave blank to use the default from `.env`)
   - **SSH Key Path:** Optional — only if you set `QUICKROBOT_API_ANSIBLE_SSHKEY` in `.env` and this node differs
4. Click **Add**

The API will SSH to the host, run a preflight check (ping + OS detection), and populate the node's hardware profile (CPU cores, RAM, GPU info). The status column should show a green indicator once ready. If it shows red or gray, check the error message for clues — most commonly:
- SSH connection refused → verify hostname and SSH is running
- Authentication failed → check SSH key setup
- Python not found → install Python 3 on the remote node

### SSH keys (if not already set up)

To avoid entering a password each time, generate an SSH key pair on the API host and copy it to each remote node:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/quickrobot_id_ed25519 -N ""
ssh-copy-id -i ~/.ssh/quickrobot_id_ed25519.pub youruser@remote-node
```

---

## 6. Scan for Models {#6-scan-for-models}

Before deploying a server, you need GGUF model files on the remote node. If you already have models placed manually (e.g., downloaded to `/opt/quickrobot/models/` or `/mnt/llama/gguf/`), quickrobot can discover them automatically.

1. Navigate to **Models** in the left sidebar
2. Click **Scan Models**
3. Select your target node from the dropdown
4. Click **Scan**

The scanner walks the model base path on the remote host, finds all `.gguf` files, and records each one with its size, hash, and last-modified timestamp. After the scan completes:

- Navigate to **Models** -> **List Models**
- Your scanned models will appear with their full path, quantization level, and file size

You can scan multiple nodes — each node has its own model inventory.

---

## 7. Create a Preset {#7-create-a-preset}

A preset is a named recipe: it combines a model file, CLI arguments, and environment variables into one reusable configuration. Presets are independent from how the binary arrives (git build vs. pre-built download).

1. Navigate to **Presets** in the left sidebar
2. Click **Add Preset**
3. Select engine type: `llama_server`
4. From the **Model** dropdown, choose one of your scanned models — the model path auto-fills
5. Enter a preset name (e.g., `QR-BONSAI-1.7B-Q2`)
6. Set environment variables in the form fields:

| Variable | Value | Purpose |
|----------|-------|---------|
| `BATCH` | `2048` | Batch size for token processing |
| `N_GPU_LAYERS` | `100` | Offload all layers to GPU (use `-1` for CPU-only) |
| `CTX_SIZE` | `4096` | Context window (adjust based on your model) |
| `FLASH_ATTN` | `1` | Enable flash attention if supported |

7. (Optional) Add CLI arguments in the **CLI Options** section:
   - `--jinja` — use Jinja prompt templates
   - `--no-context-shift` — disable context shift penalty
8. Click **Save**

The preset gets an ID automatically. For first-time testing, pick a small model (< 1 GB) to keep loading times short and verify the pipeline works before scaling up to larger models.

---

## 8. Deploy Your First llama.cpp Server {#8-deploy-your-first-llamacpp-server}

### Choose your delivery method

| Method | Speed | When to use |
|--------|-------|-------------|
| **Pre-built binary** (recommended for first deploy) | ~30 seconds | Quick testing, production with known-good builds |
| **Git build** | 10-30 minutes | Custom forks, latest llama.cpp commits, debugging |

### Deploy with pre-built binary

1. Navigate to **Instances** in the left sidebar
2. Click **Create Instance**
3. **Step 1 — Engine Type:** Select `llama_server`
4. **Step 2 — Node:** Select your remote node from the dropdown
5. **Step 3 — Binary Template:** Click "Use Pre-built Binary", then select:
   - The **CPU variant** for CPU-only inference (no GPU/Vulkan needed)
   - The **Vulkan variant** if your node has a GPU with Vulkan support
6. **Step 4 — Preset:** Select the preset you created in Section 7, or choose "Router mode" (a preset that runs without any model file — useful for testing deployment first)
7. **Step 5 — Name:** Enter a display name (e.g., `dllama1-first-server`)
8. Click **Create**

The WebUI will show the instance being created and the status column will cycle through states:
- `unconfigured` -> `configuring` -> `deploying` -> `loading` -> `running`

With a pre-built binary, deployment should complete in under a minute. With git build, expect 10-30 minutes for the first compile.

### Deploy with git build

Same steps as above, but skip Step 3 (leave "Git Build" selected). The full chain runs:
```
preflight -> install deps -> clone source -> compile -> config -> start
```

> **Note:** Only one cmake compile runs per node at a time. If you create two instances on the same node, the second will queue up and stay `unconfigured` until the first finishes compiling. Compiles on different nodes happen in parallel.

---

## 9. What Gets Deployed {#9-what-gets-deployed}

After deployment, quickrobot creates this structure on the remote node:

```
/opt/quickrobot/
├── qr-NNN-llama_server/           # Instance work directory
│   └── (files here for git builds)
├── llama.cpp/                     # Source clone (shared across all instances on this node)
│   └── build/                     # CMake build output (shared — one per node)
└── binary-templates/              # Pre-built binaries (one per template version)
    └── llama_server/
        └── b10146-ubuntu-x64-cpu/
            └── llama-server

/etc/quickrobot/
└── qr-NNN-llama_server.env        # Environment file (env vars + CLI args)

/etc/systemd/system/
└── qr-NNN-llama_server.service    # Systemd unit file for lifecycle management
```

**Key points to understand:**
- **Systemd service** at `/etc/systemd/system/qr-{id}-{engine}.service` manages start/stop/restart
- **Env file** at `/etc/quickrobot/qr-{id}-{engine}.env` contains all environment variables and the joined CLI args. Changing a preset only rewrites this file — no systemd reload needed
- **Shared source**: All llama.cpp instances on the same node share one git clone (`/opt/quickrobot/llama.cpp`). This saves disk space but means changing the `git_clone_url` affects all instances on that node
- **Shared build output**: The cmake build directory is also shared, so only one compile can run at a time per node

---

## 10. Deploy an RPC Instance on an Additional Node {#10-deploy-an-rpc-instance-on-an-additional-node}

RPC (Remote Procedure Call) instances serve model layers over gRPC. They act as distributed GPUs that a main llama.cpp server can bind to for splitting work across multiple machines.

### Deploy the RPC instance (CPU default — no thread pinning, no GPU)

1. Navigate to **Instances** -> **Create Instance**
2. **Step 1 — Engine Type:** Select `llama_rpc`
3. **Step 2 — Node:** Select a **different** node than your main server (e.g., `dllama2.lan`) — RPC nodes should be separate from the main server for cluster setups
4. **Step 3 — Binary Template:** Select the CPU RPC template for your platform
5. **Step 4 — Preset:** Select the default CPU RPC preset (the minimal resource preset with no GPU settings). This runs on plain CPU with no thread pinning — ideal for testing the cluster wiring before adding GPU presets
6. **Step 5 — Name:** Enter `dllama2-rpc-node`
7. Click **Create**

For GPU-capable RPC nodes, select a Vulkan preset instead (these configure Vulkan compute for faster gRPC serving).

Wait until the instance state changes to `running`. You can monitor progress on the Instances list page — the state column updates automatically as deployment stages complete.

**Note:** RPC instances auto-start after deployment regardless of the start-on-boot setting. They use a custom binary protocol (gRPC), not HTTP — so you cannot test them with a browser or `curl /health`.

---

## 11. Set Up a Minimal Cluster {#11-set-up-a-minimal-cluster}

With both a llama.cpp server and RPC instance(s) running, you can bind them into a cluster using the Herd page.

### Bind RPCs to the server

1. Navigate to the **Herd Cluster** page from the sidebar
2. The left panel shows all llama_server instances with their state and how many RPCs they have bound
3. Click on your server instance row to select it (the row highlights when selected)
4. In the right panel, find the **RPC Bindings** section
5. Click **Bind RPC** and choose your RPC instance(s) from the dropdown
6. Click **Confirm** — this saves the binding

### Configure the cluster layout

The Herd page also has inline controls for:
- **Split mode:** `layer` (distribute model layers across GPUs) or `tensor` (split tensors)
- **Server split:** Percentage of work the server GPU handles (set to `0` for RPCs to take all work — ideal for a minimal test)
- **Experts:** Per-RPC expert count for MoE models (leave at default for layer/tensor split modes)

For a minimal test, set split mode to `layer` and server split to `0`.

### Apply changes with debug logging

To see detailed startup logs during the next deploy:

1. In the Herd page's **CLI flags** section (under CLI Overrides), add:
   ```
   -v
   --vl 4
   ```
   (`-v` enables verbose output; `--vl 4` sets deep debug verbosity)

2. Click **Save** to persist the override

### Trigger reconfig + restart

After binding RPCs and adjusting settings:
1. Go to **Instances** -> click on your server instance to open its detail page
2. Find the **Reconfigure & Restart** button (or "Reconfigure" followed by "Restart" as separate steps)
3. Click it — the instance state will transition to `configuring` -> `deploying` -> `loading` -> `running`

### Monitor startup with log overlay

On the instance detail page, scroll to the **Logs** section and open the log view overlay. This shows:
- Service startup messages from the system journal
- llama.cpp loading progress (model file being loaded into memory)
- RPC connection attempts and any errors
- When the model load completes successfully

### If something goes wrong

If the server enters `error` state after binding RPCs:
1. **Check all bound RPCs are running** — every RPC must be in `running` state before the main server can connect to it. A single downed RPC crashes the server immediately.
2. **Open the log overlay** — connection errors show which host:port failed
3. **Unbind and retry** — go back to the Herd page, remove the problematic RPC binding, restart the server, then add RPCs back one at a time to isolate the issue

---

## 12. Debug and Monitor {#12-debug-and-monitor}

### Check instance status

On the **Instances** list page, the state column shows the current status:
- `running` — fully operational
- `loading` — model currently loading into memory (normal after start)
- `deploying` — deployment in progress
- `error` — something failed (click the instance for details)
- `stopped` — service is stopped

### View logs for any instance

1. Click on an instance name to open its detail page
2. Scroll to the **Logs** section
3. Open the log overlay to see real-time output from the systemd service

### Find your MCP token (for LLM clients)

If you need to connect an LLM client to quickrobot's MCP server:
1. Check `.quickrobot.env` on the API host — look for the `QUICKROBOT_MCP_TOKEN=QR-MCP-...` line
2. Or in the WebUI: go to **Instances**, find the MCP instance (ID 3, shown as a system-managed service), click to open its detail page — the token appears in the status display
3. Use this token when connecting your LLM client to `http://<host>:8040/sse?token=QR-MCP-...`

### Engine prompts as MCP resources

Engine prompts stored in quickrobot are available as read-only resources for LLM clients (system prompts, skill definitions). These let an LLM client fetch context without needing function calls. The operational side — create, deploy, restart, undeploy — uses function tools. This separation keeps the system clean: read data via resources, make changes via functions.

