# Ansible JSON Output Format — quickrobot Reference

> **Date:** 2026-05-12
> **Purpose:** Canonical reference for how Ansible --json output is structured
> and how `lib/lib_ansible_runner.py` normalizes it for internal use.

---

## 1. Raw Ansible --json Output Structure

When running `ansible-playbook` with `ANSIBLE_STDOUT_CALLBACK=json` (Ansible 2.10+),
the output is a JSON object with this structure:

```json
{
    "plays": [
        {
            "play": { "duration": {...}, "name": "..." },
            "tasks": [
                {
                    "task": { "name": "Check connectivity", ... },
                    "hosts": {
                        "hostname_or_alias": {
                            "_ansible_no_log": false,
                            "changed": false,
                            "ping": true,
                            "msg": {"key": "value"},
                            "failed_when_result": false,
                            "invocation": {...},
                            ...
                        }
                    },
                    "hosts_key": ["hostname_or_alias"]
                }
            ]
        }
    ],
    "contacted": [ "hostname" ],
    "dark": [],
    "playbook": "/path/to/playbook.yml",
    "stats": { ... }
}
```

**Key points:**
- `plays[].tasks[].hosts` is a **dict** keyed by hostname (NOT a list).
  This is the Ansible 2.10+ format. Older versions used `results` as a list.
- Each host entry contains the full task result: `changed`, `failed`, `msg`,
  `stdout`, `stderr`, etc.
- The `msg` field from `debug:` tasks may be **any type** — string, dict, list.

## 2. Normalized Output (parse_ansible_json)

The `parse_ansible_json()` function normalizes the raw output into a canonical
form that all internal code can rely on:

```python
result = parse_ansible_json(raw_output)
# result = {
#     "changed": bool,
#     "failed": bool,
#     "results": { ... }  # Raw parsed JSON (same as above, but guaranteed dict)
# }
```

### Guaranteed Invariant

After `parse_ansible_json()`, **every task in every play has a `results` list**:
```python
for play in result["results"]["plays"]:
    for task in play["tasks"]:
        entries = task["results"]  # Always a list of host result dicts
        for entry in entries:
            msg = entry.get("msg", "")  # May be string or dict
```

This is achieved by converting `task["hosts"]` (dict) → `task["results"]` (list)
when the hosts dict is non-empty. Code that iterates `task["results"]` will
work regardless of Ansible version.

## 3. How to Extract Data from Normalized Results

### A. Command output / debug message (stdout)
```python
for play in results.get("plays", []):
    for task in play.get("tasks", []):
        if "Output" in task.get("task", {}).get("name", ""):
            for entry in task.get("results", []):
                msg = entry.get("msg", "")
                if isinstance(msg, dict):
                    msg_str = json.dumps(msg)  # Convert dict to string
                elif isinstance(msg, str):
                    msg_str = msg
```

### B. Command stdout/stderr (command module)
```python
for play in results.get("plays", []):
    for task in play.get("tasks", []):
        if "Get" in task.get("task", {}).get("name", ""):
            for entry in task.get("results", []):
                stdout = entry.get("stdout", "")  # command module output
                stderr = entry.get("stderr", "")  # command module errors
```

### C. Failure detection
```python
if result.get("failed"):
    # At least one task failed — check which ones
    for play in results.get("plays", []):
        for task in play.get("tasks", []):
            if task.get("failed"):
                name = task.get("task", {}).get("name")
                for entry in task.get("results", []):
                    err_msg = entry.get("msg") or entry.get("stderr", "")
```

## 4. Common Gotchas

| Problem | Cause | Fix |
|---------|-------|-----|
| `task.get("results")` returns `[]` | Hosts stored under `"hosts"` key (dict) | Use normalized output from `parse_ansible_json()` |
| `msg` is a dict, not string | `debug: msg:` accepts any type | Check `isinstance(msg, dict)` and json-encode |
| `stdout` always empty | Looking in wrong field; Ansible puts command output in `stdout`, debug output in `msg` | Use correct field for the module type |
| `exit_code=0` but task failed | `failed_when` condition may set failed flag without non-zero exit | Check `task.get("failed")` not just exit_code |
| Playbook returns `[]` (empty list) | Empty playbook or syntax error | `parse_ansible_json` handles this, returns `failed=True` |

## 5. Quick Reference — All Modules and Their Output Fields

| Module | Success Output Key | Failure Output Key | Message Key |
|--------|-------------------|-------------------|-------------|
| `ping` | `ping: true` | `_ansible_rc`, `msg` | N/A |
| `command` / `shell` | `stdout`, `rc` | `stderr`, `rc`, `msg` | `msg` |
| `debug` | `msg` (any type) | N/A | `msg` |
| `file` | `changed: bool` | `msg` | `msg` |
| `copy` / `template` | `dest`, `checksum` | `msg` | `msg` |
| `systemd` | `changed: bool` | `msg` | `msg` |

---

*This document is the single source of truth for Ansible output handling in quickrobot.*
