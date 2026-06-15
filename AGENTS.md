# Project Agent Rules — quickrobot

## Scope
These rules apply to **all agents** operating within the project directory.
Agent-specific roles and workflows are defined in `.opencode/agents/*.md`.

> **See also:** `SKILL.md` for API usage; `QUICKROBOT.md` for architecture and design patterns.

---

## 1. Safety & File Handling

### Backup Before Every Edit
Before editing ANY file < 10MB in this project:
```bash
cp -n -v <filename> <filename>_backup_TIMESTAMP
```
- `-n` prevents overwrites, `-v` shows written files
- Backup files are **excluded** from the manifest (see §6)

**YAML/JSON naming:** For `.yml` and `.yaml` files, append timestamp AFTER the extension: `<name>.yml_backup_TIMESTAMP` or `<name>.yaml_backup_TIMESTAMP`. For `.json`: `<name>.json_backup_TIMESTAMP`. This keeps backups out of glob results (`*.yml`, `*.json`).

**YAML naming enforcement:** Old-style `<name>_backup_TIMESTAMP.yml` files must be renamed to `<name>.yml_backup_TIMESTAMP`. Files like `deploy_rpc_backup_20260525_231400.yml` match `*.yml` glob results and pollute playbook discovery.

**Examples:**
- WRONG: `deploy_llama_server_backup_20260606T144327.yml`
- RIGHT: `deploy_llama_server.yml_backup_20260606T144327`

**Backup file count note:** When verifying full project backups via `tar -czf`, the archived file count will be significantly lower than the source file count. This is expected: `.opencode/node_modules/` contains ~3400 files excluded from the tarball, plus `__pycache__/` and `OLD_ignore/`. A full project backup should be 5-15 MB. Trust archive integrity (exit code 0) and approximate size.

### Read Before Write
Always read the entire file before attempting an edit. Never write blindly.

### No Silent Deletion
Never use `rm`, `rm -f`, `rm -rf`, or `2>/dev/null`. Move unwanted files to `./OLD_ignore/` instead.

### File Naming
- Only **ONE dot** in filenames (e.g., `server_backup_20260511_1430.py`)
- Max **30 characters** per filename (before extension)
- Use project prefixes: `QR_*`, `app_*`, `lib_*`
- No generic names (e.g., prefer `qr_api_server.py` over `main.py`)

---

## 2. Tool Execution

### Bash Chains
Limit to **3-4 operators** (`&&`, `||`, `|`). Prefer dedicated tools over shell chaining.

### No Wildcard Kills
Never use `pkill -f "pattern"`. Query exact PID first:
```bash
ps aux | grep exact_filename.py | grep -v grep | awk '{print $2}'
```

### JSON Files
The `edit` and `write` tools handle JSON correctly. Use them directly for creating/modifying JSON files. Reserve `jq` only for filtering/path extraction in bash pipelines.

### No `cat` on Binary Files
Never use `cat` on binary files (SQLite databases, images, compiled artifacts). The binary output corrupts terminal state. Use `file <filename>` to check type first, then use the `Read` tool for text files or `xxd <file> | head` for binary inspection.

### Ansible Locale Requirement
All ansible subprocess calls MUST have `LC_ALL` and `LANG` set to `en_US.UTF-8`. The tmux session has no locale set by default — ansible fails with `unsupported locale setting` if not provided. If adding new ansible subprocess calls, always include:
```python
env["LC_ALL"] = "en_US.UTF-8"
env["LANG"] = "en_US.UTF-8"
```
Missing locale causes rc=1, empty stdout → parsed as `{"plays": []}` → API reports "ok" with no data. Silent failure.

### Ansible Output Format
The `ansible_actions.task_summary` column stores the full parsed JSON from ansible-playbook (typically 2-44KB). All playbook types now properly capture output after the locale fix. See `docs/ansible_output_format.md` for normalization details.

### Mandatory Syntax Check After Edit
After editing any file that supports syntax checking, you MUST run a single-command syntax check:
- **Python** (.py): `python3 -c "import py_compile; py_compile.compile('<filepath>', doraise=True)"`
- **JSON** (.json): `python3 -c "import json; json.load(open('<filepath>'))"`
- **YAML** (.yml/.yaml): `python3 -c "import yaml; yaml.safe_load(open('<filepath>'))"`
- **JavaScript in Python strings**: Verify `curl | grep '{{' | wc -l` — non-f-string JS must NOT contain double braces `{{`; they render literally and break JS.

---

## 3. Project Constraints

### No Git
This project has **no git repository**. Never run `git status`, `git diff`, `git log`, `git add`, `git commit`, or any git command. Use `stat` or `find -newer` for file history.

### .opencode Folder Rules
- **Agents may read and write** `.opencode/agents/*.md` (agent protocol files)
- **Agents must ignore** all other `.opencode/` subfolders: `node_modules/`, `skills/`, `context/`, etc.
- When scanning directories, exclude `.opencode/` entirely except `.opencode/agents/`
- Never install dependencies or run commands that touch `.opencode/node_modules/`

### No Dependency Installation
Never install dependencies (`pip`, `npm`, `apt`) without explicit user confirmation.

### Pip Flag Rule — ALWAYS Ask Before Using --break-system-packages
On Debian/Ubuntu with Python 3.12+, pip uses PEP 668 externally-managed-environment by default.
- If pip raises `externally-managed-environment`, you must **ask the user** before using `--break-system-packages`.
- Do NOT silently add `--break-system-packages`. Preferred alternatives: create a venv, use pipx, or ask the user.

### No Hardcoded Values in WebUI or API Responses
ALL displayed values must come from actual data sources (DB, config_override, API response):
- **IP addresses:** Read from `config_override.host`, not `"0.0.0.0"` or `"127.0.0.1"` hardcoded as fallbacks
- **Ports:** Read from `config_override.web_port`, `port_assigned`, or `engine_configs.base_port` — never hardcoded in templates
- **Hostnames:** Read from DB node records or `gethostname()` — never `"localhost"` as default fallback
- **Service binding addresses:** Must return the ACTUAL bind address, not a hardcoded string

**Rule:** If a value is displayed to the user, trace its source. If it originates from a literal string in code rather than a DB column, config key, or API response field, it is a bug.

---

## 4. Communication

- Report task completion or failure with a structured summary
- Report blockers immediately — do not continue past them
- Use varied list formats (A/B/C, 1/2/3), avoid "-" bullets
- Pure ASCII/UTF-8 English only. No emojis in any project output.

---

## 5. File Manifest Tracking

All **writable agents** must log every file modification to `./manifest.log`.

### Format (append-only, pipe-delimited)
```
<filepath> | <timestamp> | <agentname> | <backup_filename> | <reason>
```

### Timestamp Generation
Timestamps MUST be generated dynamically at log time:
```bash
TIMESTAMP=$(date +%Y-%m-%dT%H:%M:%S)
echo "<filepath> | ${TIMESTAMP} | <agentname> | <backup_filename> | <reason>" >> manifest.log
```

### Rules
- **Append only** — Never read/rewrite the manifest.
- Backup files are excluded from tracking.
- Timestamp format: ISO 8601 (`YYYY-MM-DDTHH:MM:SS`)
- Agentname must match the agent's identity (e.g., "coder", "designer")
- **Use relative paths** from project root (e.g., `lib/lib_ssh.py` not `/project/root/lib/lib_ssh.py`)
- All five fields must always be present and non-empty

### Backup Locations
- Manifest backups → `./OLD_ignore/`
- Full project tar.gz backups → `<project_root>/BACKUPS/` (or configured backup directory)

---

## 6. Server Configuration

See `QUICKROBOT.md` §Server Configuration for full details. Quick reference:

### Service Architecture
Three separate servers — all controlled via tmux (API) or API endpoint (WebUI/MCP):

| Server | File | Tmux Session | Restart |
|--------|------|-------------|---------|
| API | `quickrobot.py` | `qr_api` | `kill <pid>` or `POST /instances/1/restart_system` |
| WebUI | `quickrobot_webui.py` | API subprocess | `POST /instances/2/restart_system` |
| MCP | `engine/qr_mcp_server.py` | API subprocess | `POST /instances/3/restart_system` |

**Start:** `tmux send-keys -t qr_api 'python3 quickrobot.py' C-m`
**Check:** `tmux has-session -t qr_api 2>&1` (exit 0 = exists)
**Stop:** Query PID, then `kill <PID>`, wait 1s, verify dead.

### Configuration Hierarchy — 3 Layers
| Layer | Source | Scope | Mutable via API? |
|-------|--------|-------|------------------|
| L1 | `.quickrobot.env` (system) / `engine_configs` defaults (user) | System-wide / engine-type-wide | No |
| L2 | `engine_configs` table, keyed by `engine_type_id` | All instances of one type | Yes |
| L3 | `config_override` JSON column in `instances` table | Single instance only | Yes |

Resolution: L3 wins > L2 > L1. System-managed instances (IDs 1-3) skip L3 entirely.

### Operational Rules
- **--init Guard:** NO agent may use `--init` without explicit user confirmation. Creates fresh DB — all data lost. See `QUICKROBOT.md` §Operational Rules.
- **Process Kill Guard:** NO agent may kill the API process without explicit user confirmation. Kills running ansible-playbook subprocesses (compiles, deploys in progress). Before killing: verify no instances in `updating`, `configuring`, `deploying`, `starting`, `stopping`, `loading`, or `compiling` states.
- **Compile Verification:** Shared cmake build takes up to 30 min. SSH to check for active `cmake` processes before declaring stuck.

### Ansible Playbook Lifecycle During API Restart
Ansible-playbook subprocesses **survive** API death — they are independent processes. Builds continue to completion on remote nodes. Caveat: `_run_compile()` daemon thread uses `subprocess.run()` (blocking) — if main process exits during this, the subprocess IS terminated.

### Three Control Paths for Agent Operations

The opencode harness connects to the quickrobot project via **MCP** and exposes 14 direct function-call tools (listed in §11 API Testing Discipline). Agents have three parallel control paths that all converge on the same SQLite database — each path serves a different operational need:

A) **MCP Tools (primary, 14 functions)** — Direct function calls available in every agent's context window. These are `quickrobot_list_instances_summary`, `quickrobot_list_nodes_summary`, `quickrobot_list_models_summary`, `quickrobot_list_presets_summary`, `quickrobot_list_benchmark_prompts`, `quickrobot_run_benchmark`, `quickrobot_get_instance_status`, `quickrobot_get_model`, `quickrobot_get_preset`, `quickrobot_list_instances`, `quickrobot_list_nodes`, `quickrobot_list_models`, `quickrobot_list_presets`, `quickrobot_list_benchmark_results`. Use these for **all routine operations** — they are fast (no HTTP overhead), type-safe (parsed JSON), and support both read and write paths. Example: `quickrobot_run_benchmark(instance_id=108, prompt_id=1)` starts a benchmark without writing a single curl command.

B) **API Server (HTTP REST, port 8039)** — Full Flask API accessible via `curl` from bash. Use when you need **exact HTTP semantics** (specific status codes, headers, custom auth tokens), need to test endpoints not exposed as MCP tools, or are debugging the API layer itself. Base URL: `http://127.0.0.1:<API_PORT>/api/v1/` where `<API_PORT>` comes from `.quickrobot.env`. Example: `curl -s http://127.0.0.1:8039/api/v1/app/status` verifies the API process is alive and returns PID + uptime.

C) **WebUI (browser frontend, port 8038)** — Flask app serving HTML/JS SPA. Use when verifying **UI rendering** or testing end-to-end user flows. Also curl-able for quick health checks: `curl -s http://127.0.0.1:8038/` returns the redirect page.

**Path selection rules:**
- Default to **MCP tools** — they are the fastest, most reliable, and always available in context.
- Use **API** when you need raw HTTP behavior (status codes, response headers, auth token validation).
- Use **WebUI** only when the question is specifically about rendered output or user interaction.
- All three paths share the same DB — reads from one are immediately visible to the others (no caching layer between them).
- Write operations via MCP tools (`run_benchmark`, etc.) have the same effect as equivalent API calls — the tool IS a thin wrapper around the API handler.

---

## 7. Ansible Template Gotchas

### Jinja2 Whitespace in Ansible
Ansible's `template` module consumes **ALL surrounding whitespace** around Jinja2 control tags (`{% if %}`, `{% endif %}`, `{% for %}`). This means:

```jinja2
# DANGEROUS — tags consume preceding newlines:
{% if merged_env %}EnvironmentFile={{ env_file }}
{% endif %}ExecStart=...Vulkan0   # newline after endif consumed!
```

**Fix:** Pre-compute lines via `set_fact` in a separate Ansible task before template render:
```yaml
- name: Build ExecStart line
  set_fact:
    exec_start_line: >-
      ExecStart={{ binary_path }} -H {{ host | default('0.0.0.0') }}
      -p {{ port }} -d {{ device | default('CPU') }}
      {% if cli_opts %} {{ cli_opts | join(' ') }}{% endif %}
```

### Extra Vars — No `from_json` Needed
Ansible 2.10+ **auto-parses JSON** passed via `--extra-vars`. Extra vars arrive as proper Python types — do NOT use `| from_json` on them. Using `from_json` on an already-parsed list causes errors.

**Correct pattern:** Use extra_vars directly:
```yaml
{% if merged_cli_opts is defined and merged_cli_opts %} {{ merged_cli_opts | join(' ') }}{% endif %}
loop: "{{ instance_env_vars | default([]) }}"
```

**When `from_json` IS needed:** Only when a value was explicitly stored as a JSON-encoded string (e.g., in `config_override` JSON column). Check type first:
```yaml
- debug: msg="type={{ merged_cli_opts | type_debug }}"
# If "str" → use from_json; if "list" → use directly
```

### ExecStart Last in [Service]
Put `ExecStart=` as the last configurable directive in `[Service]`, followed by an empty blank line, then static lines. The empty line prevents Ansible from consuming the final newline.

---

## 8. Forbidden Actions

The following are forbidden for ALL agents:
- `rm`, `rm -f`, `rm -rf` (anywhere, any scope)
- Wildcard process kills (`pkill -f`, `killall`)
- Installing dependencies without user confirmation
- Modifying files outside the project directory without explicit authorization
- Restoring full project backups from tar.gz without Design Agent approval

### No Restore From Backup Without Explicit Confirmation
NO agent may restore ANY file from ANY backup location without explicit user confirmation. The agent must state which file(s), from where, and why, and wait for approval.

### NEVER DELETE OLD BACKUPS — Absolute Rule
**UNDER NO CIRCUMSTANCES EVER delete old backup files.** Applies to ALL locations:
- Full project tar.gz backups (configured backup directory)
- `./OLD_ignore/` — old-style backup files
- `./backups/` — staging directory backups
- `./data/_backups/` — database backups
- Any `_backup_*` file anywhere in the project

Forbidden: `rm`, `rm -f`, `rm -rf`, `find ... -delete`, `find ... -exec rm`, glob cleanup like `rm OLD_ignore/*`. The user may clean backups manually.

---

## 9. SSH Host Key Checking Policy

**Do NOT use `-o StrictHostKeyChecking=no` as default.** It silently accepts any host key change (MITM attack vector). Preferred order:
1. `StrictHostKeyChecking=yes` — prompts on first connect, verifies on subsequent
2. `StrictHostKeyChecking=accept-new` — headless-friendly, auto-accepts new hosts, rejects changed keys
3. `StrictHostKeyChecking=ask` — interactive

**`accept-new` is FORBIDDEN in runtime code unless explicitly authorized.** Per-user per-case approval required.

### SSH Hostname Preference
Prefer **DNS names** over raw IPs for SSH connections and Ansible inventory:
- Use `ssh user@hostname.domain` instead of `ssh user@192.168.1.42`
- DNS names are stable even if IP changes (DHCP reassignment)
- The Ansible dynamic inventory already uses `.lan` FQDNs from the nodes table

---

## 10. Coding Integration Insights

### Playbook → API → DB → WebUI Data Flow
Every feature touches 4 layers that must stay in sync:
1. **Ansible playbook** — gathers data from remote nodes, writes YAML with `@playbook_id:` header + checksum in seed
2. **API handler** (`quickrobot/routes_*.py`) — parses playbook result JSON, calls DB adapter
3. **DB adapter** (`db/adapters/*.py`) — INSERT/UPDATE with dynamic column detection (`PRAGMA table_info`)
4. **WebUI** (`webui/*.html`) — renders API response via JavaScript

**Integration rule:** When adding a new field, update ALL 4 layers. Changing DB schema without updating seed file → fresh `--init` loses the new data. Seed file must be regenerated after any schema change.

### Timestamp Handling — UTC Everywhere
- **SQLite** `strftime('%Y-%m-%dT%H:%M:%S','now')` returns UTC (no 'Z' suffix)
- **Python** `_dt.now(_dt.timezone.utc).replace(tzinfo=None)` strips tzinfo for naive comparison
- **WebUI** `qrFmtRelative()` receives naive UTC strings, displays relative time in user's browser timezone

**Gotcha:** `created_at` default captures **import timestamp**, not file age. For tracking download age, capture file mtime from playbook and store in a separate column.

---

## 11. API Testing Discipline

### Rule — Read State Before Writing State
Before ANY write action (deploy, build, create, update-build, reboot, apt-update):
1. **Read current state** — check instance `state`, node `is_active`, `node_build_state`
2. **Plan** — based on state, decide if action makes sense (running + no config change → skip deploy; node_build_state == "running" → skip builds)
3. **Execute ONE action**, verify result

### Rule — One Action Per Test Command
Do NOT chain multiple write actions in a single command. Each test:
```bash
# GOOD — single action, clear result
curl -s -X POST http://127.0.0.1:<API_PORT>/api/v1/instances/<id>/reconfigure

# BAD — three writes in one shot, cascading side effects
echo "=== 1 ===" && curl -s POST ... && echo "=== 2 ===" && curl -s POST ...
```

### Rule — Use Inactive Nodes for Destructive Tests
When testing actions that change state (deploy, rebuild, reboot), use nodes known to be inactive (`is_active=0`) with no live instances.

### Rule — Ask Before Bulk Testing
If the test involves more than one write action, or targets a live/active node, **ask the user first**.

### Rule — No Testing Without Verification
After every test action: read the response, check resulting state. If unexpected, STOP and report — do NOT chain more actions.

### API Endpoint Quick Reference
**Base URL:** `http://127.0.0.1:<API_PORT>/api/v1/` where `<API_PORT>` = `QUICKROBOT_API_PORT` from `.quickrobot.env`.

#### Core CRUD Endpoints
| Purpose | Method | Path | Response key |
|---------|--------|------|-------------|
| List instances | GET | `/instances` | `items` |
| Get instance | GET | `/instances/<id>` | `data` |
| Create instance | POST | `/instances` | `data` |
| Update instance | PUT | `/instances/<id>` | `data` |
| Delete instance | DELETE | `/instances/<id>` | `data` |
| Deploy instance | POST | `/instances/<id>/deploy` | `data` |
| Start/Stop/Restart | POST | `/instances/<id>/start`, `stop`, `restart` | `data` |
| Undeploy instance | POST | `/instances/<id>/undeploy` | `data` |
| Restart system instance | POST | `/instances/<id>/restart_system` | `data` |
| List nodes | GET | `/nodes` | `items` |
| Get node | GET | `/nodes/<id>` | `data` |
| Create node | POST | `/nodes` | `data` |
| Node actions | POST | `/nodes/<id>/reboot`, `shutdown`, `apt-update`, `discover` | `data` |

#### Engine & Preset Endpoints
| Purpose | Method | Path |
|---------|--------|------|
| List engine configs | GET | `/engine/<type>/config` |
| Set engine config | PUT | `/engine/<type>/config/<key>` |
| Delete engine config | DELETE | `/engine/<type>/config/<key>` |
| List presets | GET | `/engine/<type>/presets` |
| Create preset | POST | `/engine/<type>/presets` |
| Update preset | PUT | `/engine/<type>/presets/<id>` |
| Delete preset | DELETE | `/engine/<type>/presets/<id>` |
| List models | GET | `/models` or `/engine/<type>/models` |
| Scan models | POST | `/models/scan?node=<id>` |

#### Other Endpoints
| Purpose | Method | Path |
|---------|--------|------|
| Benchmark run | POST | `/benchmarks/run` |
| List benchmarks | GET | `/benchmarks/results?instance_id=<id>` |
| Ansible actions | GET | `/ansible_actions?instance_id=<id>&limit=5` |
| Playbooks list | GET | `/playbooks` |
| Health check | POST | `/health/check` |
| App status | GET | `/app/status` |

#### Response Format
- **Single resource:** `{ "status": "ok", "data": { ... } }` → access via `resp.get("data", {})`
- **List resources:** `{ "status": "ok", "total": N, "items": [...] }` → access via `resp.get("items", [])`
- **Error:** `{ "status": "error", "code": "...", "message": "..." }`

**Always check `response.get("status")` first.** A 404 returns error status with `RESOURCE_NOT_FOUND` code — do NOT interpret empty `items` array as "zero results" when the response is actually an error.

#### Engine Names (NOT IDs) — Use in API Paths
| Name | ID | Usage |
|------|-----|-------|
| quickrobot-api | 1 | `/engine/quickrobot-api/config/*` |
| quickrobot-webui | 2 | `/engine/quickrobot-webui/config/*` |
| quickrobot-mcp | 3 | `/engine/quickrobot-mcp/config/*` |
| universal | 11 | `/engine/universal/config/*` |
| subprocess | 12 | `/engine/subprocess/config/*` |
| llama_server | 21 | `/engine/llama_server/config/*` |
| llama_rpc | 22 | `/engine/llama_rpc/config/*` |
| iperf3 | 31 | `/engine/iperf3/config/*` |

**⚠ Old names (`qr_api`, `qr_webui`, `qr_mcp`) return HTTP 400. Always use DB engine names above.**

#### Response Parsing Patterns
- Instance objects: fields are `engine_type_name` (NOT `engine_type`) and `node_hostname` (NOT `node_name`)
- Single-resource parsing: `d = json.load(r); i = d.get("data", {}); print(i.get("state"))`
- List parsing: `d = json.load(r); [print(f'  ID={i["id"]} state={i["state"]}') for i in d.get("items", [])]`
- System-managed engine config endpoints: `GET /api/v1/engines/quickrobot-webui/settings` (host, web_port), `GET /api/v1/engines/quickrobot-api/status` (pid, rss_bytes, uptime)

---

## 12. Database Reference

### DB File Location
- **File:** `data/quickrobot.db` (relative to project root, ~6-13 MB)
- **NOT** `qr.db` — that's a 0-byte decoy file sometimes created by hand
- **Query:** `sqlite3 data/quickrobot.db "SQL"`
- **Backup dir:** `data/_backups/`

### All Tables (16)
| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `engine_types` | Engine type definitions | id, name, display_name, capabilities (JSON), base_port |
| `engine_configs` | Per-engine-type config | engine_type_id, key, value, default_value |
| `nodes` | Remote node inventory | id, name, hostname, ipv4_address, ping_state, is_active |
| `instances` | Deployed instances | id, name, engine_type_id, node_id, state, port_assigned, config_override (JSON) |
| `engine_models` | Discovered models | id, engine_type_id, name, model_path, is_active, preset_count |
| `engine_presets` | Engine presets | id, engine_type_id, name, category, config_template (JSON), model_id |
| `benchmark_prompts` | Prompt templates for benchmarks | id, name, content, max_tokens |
| `benchmark_results` | Benchmark run results | id, instance_id, prompt_id, success, tokens_per_sec, response_json |
| `playbook_registry` | Playbook metadata + checksums | id, file_path, playbook_id, checksum_sha256, file_size |
| `ansible_actions` | Ansible playbook execution logs | id, node_id, instance_id, action_type, task_summary (JSON), status |
| `qr_actions` | Framework-level operation logs | id, action_type, node_id, instance_id, actor, details (JSON) |
| `groups` / `group_members` | Instance grouping (many-to-many) | groups: id, name; group_members: group_id, instance_id |
| `node_configs` | Per-node config overrides | node_id, key, value |
| `config_global` | Global system config | key, value |
| `applied_migrations` | Migration version tracking | migration_name, applied_at |
| `request_log` | HTTP request audit log | id, method, path, status, ip, timestamp |
| `instance_logs` | Per-instance log entries | id, instance_id, level, message, created_at |

### Engine Type IDs (SOT in `lib/qr_engine_ids.py`)
| ID | Name | Display | Port Default | Lifecycle |
|----|------|---------|-------------|-----------|
| 1 | quickrobot-api | Quickrobot API | 8039 | tmux `qr_api` |
| 2 | quickrobot-webui | Quickrobot WebUI | 8038 | subprocess (PID-in-DB) |
| 3 | quickrobot-mcp | Quickrobot MCP Server | 8040 | subprocess (PID-in-DB) |
| 11 | universal | Universal Engine | 0 | systemd playbook |
| 12 | subprocess | Subprocess | — | PIDs tracked in DB |
| 21 | llama_server | LLAMA.cpp | 8080 | systemd playbook |
| 22 | llama_rpc | LLAMA.RPC Server | 50052 | systemd playbook |
| 31 | iperf3 | Iperf3 | 9900 | systemd playbook |

### Seed File
- **Location:** `data/_seed/seed_v006.sql` (relative to project root)
- **Format:** `INSERT OR REPLACE` statements — fully idempotent
- **Contents:** engine_types, node stub, engine_configs, benchmark_prompts, playbook_registry, models, presets, sample instance
- **Verification keys in `.quickrobot.env`:**
  - `QUICKROBOT_SEED_CHECKSUM` — SHA256 of the file
  - `QUICKROBOT_SEED_FILESIZE` — file size in bytes
  - `QUICKROBOT_SEED_MAX_ID` — max ID range (default 1000)
- **--init flow:** pre-validate checksum+size → backup old DB → delete → create fresh → run migrations → import seed → auto-provision system instances

### SQLite Query Tips
- **List tables:** `sqlite3 data/quickrobot.db ".tables"`
- **Schema for one table:** `sqlite3 data/quickrobot.db "PRAGMA table_info(table_name);"`
- **Row count:** `sqlite3 data/quickrobot.db "SELECT COUNT(*) FROM table_name;"`
- **Filter active:** `sqlite3 data/quickrobot.db "SELECT * FROM engine_models WHERE is_active=1 ORDER BY id;"`
- **JSON columns:** `config_override`, `capabilities`, `task_summary` — stored as JSON strings, read with `json_extract()` or load via Python

### Common Patterns for DB Tasks
A) Export data to seed-compatible SQL: use Python `sqlite3` with `row_factory=sqlite3.Row`, format values with type-aware quoting (integers unquoted, strings single-quoted, None→NULL keyword).
B) Verify SQL syntax: `sqlite3 /tmp/test.db < seed_file.sql` — "no such table" errors are expected on empty DB; look for actual parse errors.
C) Checksum update after edit: `sha256sum data/_seed/seed_v006.sql` + `wc -c data/_seed/seed_v006.sql`, then update `.quickrobot.env`.

---

## 13. Cross-Reference Map

| Topic | See |
|-------|-----|
| Database structure, tables, engine IDs, seed file | §12 (this section) |
| API endpoint reference + MCP tools | `SKILL.md` |
| Architecture, merge chains, playbook registry | `QUICKROBOT.md` |
| Task list (open items) | `docs/TODO.md` |
| Task list (full history) | `docs/TODO_v005.md` |
| Ansible JSON format details | `docs/ansible_output_format.md` |
| Sortable table pattern | `docs/sortable_tables.md` |
| Changelog | `CHANGELOG.md`, `CHANGELOG_v005.md` |

## CODE FREEZE IN PROGRESS: NO SOURCE EDITS OR CODE MODS WITHOUT EXPLICIT USER AUTH!
