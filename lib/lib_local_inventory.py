# Copyright 2026 comchris quickrobot .de project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Quickrobot — Localhost hardware inventory gatherer.

Gathers CPU, RAM, disk, GPU, and OS info from the local machine
without requiring root privileges.  Each command is wrapped in
try/except so a single failure does not cascade into the caller.

Output dict keys match validate.yml task output:
    cpu_cores (int), ram_mb (int), os (str),
    gpu_name (str|None), gpu_type (str|None), gpu_memory_mb (int|None),
    fs_free_gb (float), available_devices (list)
"""

import subprocess as _subp


def _run(cmd, default="", shell=True, timeout=10):
    """Run a shell command and return trimmed stdout or default on failure."""
    try:
        r = _subp.run(
            cmd, capture_output=True, text=True, shell=shell, timeout=timeout,
        )
        out = (r.stdout or "").strip()
        return out if out else default
    except Exception:
        return default


def gather_local_inventory():
    """Gather hardware inventory from the local machine (no root required).

    Runs a set of lightweight commands to collect CPU, RAM, disk, OS,
    and GPU information.  Each command is isolated so one failure does
    not affect others.

    Returns:
        dict with keys compatible with lib_ansible_runner.validate_node():
            cpu_cores (int|None), ram_mb (int|None), os (str|None),
            gpu_name (str|None), gpu_type (str|None), gpu_memory_mb (int|None),
            fs_free_gb (float|None), available_devices (list)
    """
    inventory = {
        "cpu_cores": None,
        "ram_mb": None,
        "os": None,
        "gpu_name": None,
        "gpu_type": None,
        "gpu_memory_mb": None,
        "fs_free_gb": None,
        "available_devices": [],
    }

    # --- CPU cores ----------------------------------------------------------
    nproc = _run("nproc", default="0")
    try:
        inventory["cpu_cores"] = max(1, int(nproc))
    except (ValueError, TypeError):
        pass  # Keep None if parsing fails

    # --- RAM (MB) -----------------------------------------------------------
    ram_line = _run(
        "free -m | awk 'NR==2{printf \"%d\", $2}'", default="0",
    )
    try:
        inventory["ram_mb"] = max(0, int(ram_line))
    except (ValueError, TypeError):
        pass

    # --- OS identifier ------------------------------------------------------
    os_name = _run("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"'")
    if not os_name:
        os_name = _run("uname -s", default="unknown")
    if os_name:
        inventory["os"] = os_name

    # --- Disk free (GB on /) ------------------------------------------------
    fs_line = _run(
        "df -BG / | awk 'NR==2{gsub(/G/,\"\",$4); print $4}'", default="0",
    )
    try:
        inventory["fs_free_gb"] = float(fs_line) / 1.0
    except (ValueError, TypeError):
        pass

    # --- GPU detection ------------------------------------------------------
    # Primary: nvidia-smi (may return empty if no NVIDIA GPU or no driver)
    nvidia_out = _run(
        "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits",
    )
    if nvidia_out and "N/A" not in nvidia_out:
        for line in nvidia_out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                gpu_name, gpu_mem = parts[0], parts[1]
                try:
                    inventory["gpu_name"] = gpu_name
                    inventory["gpu_type"] = "nvidia"
                    inventory["gpu_memory_mb"] = int(float(gpu_mem))
                except (ValueError, TypeError):
                    pass
                break
    else:
        # Fallback: lspci VGA / 3D controller detection
        pci_out = _run("lspci 2>/dev/null | grep -iE 'vga|3d|display'")
        if pci_out:
            inventory["gpu_name"] = pci_out.splitlines()[0].strip()
            # Try to infer type from lspci description
            desc_lower = pci_out.lower()
            if "nvidia" in desc_lower or "geforce" in desc_lower or " Quadro" in desc_lower or "tesla" in desc_lower:
                inventory["gpu_type"] = "nvidia"
            elif "amd" in desc_lower or "radeon" in desc_lower:
                inventory["gpu_type"] = "amd"
            elif "intel" in desc_lower:
                inventory["gpu_type"] = "intel"
            else:
                inventory["gpu_type"] = "other"
            # memory.total not available from lspci without root — leave as None

    return inventory


def gather_local_hostname():
    """Resolve the actual machine hostname.

    Tries 'hostname' command first, falls back to Python socket.
    Returns a string (never None).
    """
    name = _run("hostname", default="")
    if not name:
        import socket
        try:
            name = socket.gethostname()
        except Exception:
            name = "localhost"
    return name
