# quickrobot — Changelog

> **Note:** Previous release changelogs are versioned separately:
> - `CHANGELOG_v004.md` — v0.04 and earlier
> - `CHANGELOG_v005.md` — v0.05 to v0.06
>
> Entries below are for the current release cycle onward.

---

## 2026-06-15 — GPU OVERRIDE REGRESSION FIX (GPU-OVERRIDE-FIX)

### Bug: Herd "GPU Override" field stored as unrecognized CLI flag
**Problem:** Setting GPU Override in the RPC cluster herd view saved `-dev-override <value>` pairs into the instance's `cli_flags` column. This produced a literal `-dev-override Vulkan0` token in the systemd ExecStart line that `llama-server` does not recognize — causing potential silent failures or ignored flags on deploy. Additionally, the extraction logic in `build_llama_server_env()` read from `preset_cli_opts` (the preset's config_template), not from instance cli_flags, so even when the value was stored it was never actually consumed for device list construction.

**Root cause:** The GPU Override field used a dedicated CLI flag (`-dev-override`) as its storage mechanism, conflating configuration data with command-line argument injection. The extraction path read from the wrong source (preset cli_opts instead of instance cli_flags), so gpu_override was always None during build while the literal `-dev-override` token ended up in the final ExecStart.

**Fix:**
- **Storage:** GPU Override now stored via `config_override.LLAMA_ARG_DEVICE` (same pattern as other ENV overrides like LLAMA_ARG_BATCH, LLAMA_ARG_CTX_SIZE). No new DB columns — uses existing config_override JSON column.
- **Extraction (build_llama_server_env):** Changed from `while "-dev-override" in preset_cli_opts` to reading `merged["env"]["LLAMA_ARG_DEVICE"]` (Layer 5: config_override, highest priority in merge chain).
- **Standalone support:** Added `else` branch for llama-server instances without RPC bindings — now constructs `-dev <value>` flag when gpu_override or preset base_dev is set. Previously standalone instances got no -dev flag at all.
- **Cluster summary fix:** `get_cluster_summary()` now reads gpu_override from config_override (same source as build), not from cli_flags extraction.

**Result:** GPU Override now correctly produces `-dev Vulkan0,RPC0,RPC1` (cluster) or `-dev Vulkan0` (standalone) with zero stray CLI tokens. Preset's gpu_device still works as fallback when GPU Override is empty.

**Files Modified (3):**
| File | Change |
|------|--------|
| `webui/rpccluster.html` | Replaced `herdSaveGpuOverride()`: GET config_override → merge LLAMA_ARG_DEVICE → PUT config_override (was: fetch cli_flags, push -dev-override, save cli_flags) (+12 -10) |
| `lib/lib_cluster_env_builder.py` | L208-209: gpu_override reads from merged env; L223-232: removed -dev-override extraction; L253-258: added standalone else branch; L476-484: summary reads config_override (-8 +9) |

**Before:** `ExecStart: ... -dev RPC0,RPC1 -dev-override Vulkan0` (unrecognized flag). gpu_override always None in preview.
**After:** `ExecStart: ... -dev Vulkan0,RPC0,RPC1` (clean, no stray tokens). Preview shows correct value and -dev string.

---

## 2026-06-15 — CONFIG_OVERRIDE EMPTY-STRING DELETE (CO-DELETE-FIX)

### Bug: `PUT /instances/<id>` with empty config_override `{}` doesn't clear keys
**Problem:** Setting GPU Override to "Vulkan0" saved it into `config_override` and it was impossible to unset because `PUT /instances/{id} {"config_override": {}}` merged nothing (old key survived). Users saw "stuck" values — the GPU Override field would show the old value and couldn't be cleared.

**Root cause:** `api_update_instance()` at L544-557 used `new_override.update(co_in)` which always adds/overwrites keys, never removes them. Empty dict `{}` merges zero keys, leaving all existing ones intact.

**Fix:** Changed merge logic in `api_update_instance()` to treat empty string `""` as "delete this key":
- `"LLAMA_ARG_DEVICE": ""` → removes LLAMA_ARG_DEVICE from config_override entirely
- `"LLAMA_ARG_DEVICE": "Vulkan0"` → sets the value (unchanged behavior)
- `{}` → merges nothing, keeps existing config (unchanged behavior)

**Result:** Users can clear GPU Override via herd UI by sending empty value. `PUT /instances/{id} {"config_override": {"LLAMA_ARG_DEVICE": ""}}` → config_override becomes `{}`, gpu_override returns None.

**Files Modified (1):**
| File | Change |
|------|--------|
| `quickrobot/routes_instances.py` | L544-557: merge loop checks for `v == ""` and calls `new_override.pop(k, None)` instead of update (+8 -2) |

**Before:** `PUT {config_override: {}}` → config_override stays `{LLAMA_ARG_DEVICE: "Vulkan0"}` (key survives).
**After:** `PUT {config_override: {LLAMA_ARG_DEVICE: ""}}` → config_override becomes `{}` (key deleted).

---

## 2026-06-15 — SESSION SUMMARY: GPU OVERRIDE FIX + NODE CREATION TEST

### Operations Performed
A) **GPU Override regression fix** (KB6) — `webui/rpccluster.html`, `lib/lib_cluster_env_builder.py` (2 files, 6 sub-edits). GPU Override stored in `config_override.LLAMA_ARG_DEVICE` instead of `cli_flags`. Extraction reads from merged env (Layer 5). Standalone instances now get `-dev` flag. Verified via herd API: `--rpc dllama1:50052,dllama2:50052 -dev Vulkan0,RPC0,RPC1`.

B) **Config override delete fix** — `quickrobot/routes_instances.py`. Empty string `""` now deletes keys from config_override. Previously `{}` merge preserved old keys, making GPU Override appear "stuck".

C) **Node/instance creation test** via MCP tools (`quickrobot_list_nodes_summary`, `quickrobot_list_instances_summary`, etc.):
- Created 3 nodes: dllama1 (id=8), dllama2 (id=9), dllama3 (id=10)
- Cleaned old `qr-*.service` files on dllama3
- Created 3 RPC instances (preset 14, 2-thread): ids 100/101/102 — all running on port 50052
- Created 3 llama-servers (preset 100): ids 103/104/105 — all running on port 8080
- Tested GPU Override via herd API: bind RPCs → set GPU Override to Vulkan0 → preview shows correct `-dev` flag with GPU override before RPC refs
- Verified empty-string clear works: `PUT {config_override: {"LLAMA_ARG_DEVICE": ""}}` → key deleted, gpu_override=None

### Playwright Browser Test
- Herd page loads with 0 console errors/warnings
- `herdSaveGpuOverride()` function exists in browser scope
- GPU Override input populated from deploy-preview `gpu_override` field

---

## 2026-06-15 — CODEBASE SCAN + STALE BACKUP CLEANUP (SCAN-FINDINGS)

### Operations Performed
A) **Stale backup cleanup:** Moved 8 `_backup_*` files from project root and subdirs to `OLD_ignore/`. Verified full backup integrity (231/231 files). Logged in manifest.log.

B) **Read-only codebase scan** across all Python source, WebUI templates, engine implementations, and DB adapters for:
- Hardcoded ports/IPs that should come from config SOT
- Forbidden patterns (`rm -rf`, `pkill -f` in executable code)
- Double-brace `{{` in JS embedded in Python strings
- Missing locale settings on subprocess calls

### Findings Logged to TODO.md (0.07 section)
- **SCAN-H1 (MEDIUM):** `db/adapters/instances.py:761` hardcodes `base_port = 8080` as universal fallback for port allocation. Should look up per-engine base_port from CAPABILITIES/QR_ENGINE_PORT_DEFAULTS. iperf3 and llama_rpc would get wrong base if no engine_configs entry exists.
- **SCAN-H5 (LOW):** System engine port defaults (`8041`, `8042`) duplicated in `lib_system_engine.py:_validate_env_config()` vs `qr_engine_ids.py:QR_ENGINE_PORT_DEFAULTS`. Currently matches but risks drift. Fix: import from SOT instead of duplicating.
- No double-brace JS issues found. No forbidden patterns in executable code. All subprocess locale settings correct.

### Files Modified (1)
| File | Change |
|------|--------|
| `docs/TODO.md` | Added SCAN-H1 and SCAN-H5 entries to 0.07 section (+38 lines) |
