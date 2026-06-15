-- migrations/000_base_006.sql
-- Base schema for quickrobot v0.06 (consolidated from 000_base_004 + migrations 001, 002, 003, 005)
-- Run this file to create a fresh database from scratch.
-- Consolidated: 2026-06-07 v0.04 base + migration 025/026/027 (playbook_registry fixes) + migration 001 (node_build_state) + migration 002 (adapter→draft rename, is_active, host_type) + migration 003 (rpc→llama_rpc rename in schema) + migration 005 (qr_actions timing columns).
-- Migration 003 engine name update (data-only) was moved to seed file for v0.06+ fresh installs.

PRAGMA foreign_keys = OFF;

-- =============================================================================
-- TABLES (ordered by dependency)
-- =============================================================================

-- engine_types: registered engine types (auto-seeded at startup, not in seed file)
CREATE TABLE IF NOT EXISTS engine_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    module_path TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    enabled INTEGER DEFAULT 1 CHECK(enabled IN (0,1)),
    capabilities TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- nodes: remote host definitions with IPv6 support
CREATE TABLE IF NOT EXISTS "nodes" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'ansible' CHECK(transport IN ('ansible','ssh')),
    ansible_inventory_host TEXT,
    ansible_user TEXT,
    ansible_port INTEGER DEFAULT 22,
    ansible_key_path TEXT,
    status TEXT DEFAULT 'unknown' CHECK(status IN ('active','inactive','error','unknown')),
    status_reason TEXT DEFAULT '',
    capabilities TEXT DEFAULT '{}',
    available_devices TEXT DEFAULT '[]',
    cpu_cores INTEGER DEFAULT NULL,
    ram_mb INTEGER DEFAULT NULL,
    os TEXT DEFAULT 'unknown',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    fs_free_gb REAL,
    -- Operational ping status: online (reachable), offline (unreachable), disabled (ping not configured)
    ping_state TEXT DEFAULT 'disabled' CHECK(ping_state IN ('online', 'offline', 'disabled')),
    -- Admin toggle: 1 = show in filters, 0 = hide from filters. Default 1 for existing hosts.
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    -- IPv6 support (migration 040)
    ipv4_address TEXT,
    ipv6_address TEXT,
    -- Default model root path for this node (overridable via PUT /nodes/<id>)
    model_base_path TEXT DEFAULT '/mnt/llama/gguf/models',
    -- Per-node shared build coordination (cmake build dir lock)
    node_build_state TEXT DEFAULT 'idle',
    -- Host type: baremetal/docker/lxc/vm_proxmox/vm_qemu/other
    host_type TEXT DEFAULT '' CHECK(host_type IN ('', 'baremetal', 'docker', 'lxc', 'vm'))
);

-- instances: deployed service instances
CREATE TABLE IF NOT EXISTS "instances" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    preset_id INTEGER REFERENCES engine_presets(id),
    config_override TEXT DEFAULT '{}',
    transport TEXT NOT NULL DEFAULT 'ansible' CHECK(transport IN ('ansible','ssh')),
    ansible_playbook TEXT,
    ansible_vars TEXT DEFAULT '{}',
    ansible_extra_args TEXT DEFAULT '',
    -- State machine states (updated by migrations 025+)
    state TEXT DEFAULT 'unconfigured' CHECK(state IN (
        'unconfigured','configuring','deploying','running','stopping','stopped','error',
        'test_mode','starting','deployed','timeout','compiling','build_error',
        'updating','loading'
    )),
    last_state_change TEXT,
    port_assigned INTEGER,
    pid_last_known INTEGER,
    uptime_seconds INTEGER DEFAULT 0,
    system_managed INTEGER DEFAULT 0 CHECK(system_managed IN (0,1)),
    rss_bytes INTEGER DEFAULT 0,
    gpu_device TEXT DEFAULT NULL,
    -- UUID per instance for collision prevention (migration 009)
    instance_uuid TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(6)))),
    build_number TEXT,
    start_after_deploy INTEGER DEFAULT 0 CHECK(start_after_deploy IN (0,1)),
    rpc_bind_ids TEXT DEFAULT '[]' CHECK(rpc_bind_ids IS NULL OR json_valid(rpc_bind_ids)),
    split_mode TEXT DEFAULT 'layer' CHECK(split_mode IN ('layer','row','tensor',NULL)),
    tensor_split TEXT,
    split INTEGER DEFAULT 0,
    -- Node columns (computed at runtime for display)
    node_name TEXT,
    node_hostname TEXT,
    node_build_state TEXT DEFAULT 'idle',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    start_on_boot TEXT DEFAULT 'true' CHECK(start_on_boot IN ('true','false')),
    experts INTEGER DEFAULT 0 CHECK(experts >= 0 AND experts <= 100),
    draft INTEGER DEFAULT 0 CHECK(draft >= 0 AND draft <= 100),
    cli_flags TEXT DEFAULT '[]',
    UNIQUE(name, node_id)
);

-- engine_models: model files on nodes (gguf, mmproj, adapters)
CREATE TABLE IF NOT EXISTS engine_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    name TEXT NOT NULL,
    model_path TEXT NOT NULL,
    mmproj_path TEXT,
    draft_model_path TEXT,
    quantization TEXT,
    size_bytes INTEGER,
    last_modified TEXT,
    host_id INTEGER REFERENCES nodes(id),
    is_sharded INTEGER DEFAULT 0,
    total_shards INTEGER,
    discovered INTEGER DEFAULT 1 CHECK(discovered IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    model_params TEXT NOT NULL DEFAULT '{}',
    preset_count INTEGER NOT NULL DEFAULT 0,
    sha256_model TEXT,
    sha256_mmproj TEXT,
    sha256_draft TEXT,
    sha256_verified_at_model TEXT,
    sha256_verified_at_mmproj TEXT,
    sha256_verified_at_draft TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    UNIQUE(engine_type_id, model_path)
);

-- engine_presets: reusable configuration templates
CREATE TABLE IF NOT EXISTS engine_presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    name TEXT NOT NULL,
    category TEXT DEFAULT 'default',
    config_template TEXT NOT NULL DEFAULT '{}',
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    model_id INTEGER DEFAULT NULL,
    gpu_device TEXT DEFAULT NULL,
    UNIQUE(engine_type_id, name)
);

-- engine_configs: per-engine-type configuration defaults
CREATE TABLE IF NOT EXISTS engine_configs (
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    description TEXT DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    default_value TEXT,
    polling_interval_local_sec TEXT DEFAULT "10",
    polling_interval_remote_sec TEXT DEFAULT "600",
    refresh_interval_default_sec TEXT DEFAULT "30",
    PRIMARY KEY (engine_type_id, key)
);

-- benchmark_prompts: reusable benchmark test prompts
CREATE TABLE IF NOT EXISTS benchmark_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK(LENGTH(name) > 0),
    content TEXT NOT NULL CHECK(LENGTH(content) > 0),
    max_tokens INTEGER NOT NULL DEFAULT 20,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- benchmark_prompts: preseeded benchmark test prompts
INSERT OR IGNORE INTO benchmark_prompts (id, name, content, max_tokens) VALUES (1, 'Count10', 'Count from 1 to 10. Do not argue.', 50);
INSERT OR IGNORE INTO benchmark_prompts (id, name, content, max_tokens) VALUES (2, 'Count1000', 'Count from 1 to 1000. Do not argue.', 3000);

-- benchmark_results: benchmark execution records with FK cascade to prompts
CREATE TABLE IF NOT EXISTS benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(6)))),
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    prompt_id INTEGER NOT NULL REFERENCES benchmark_prompts(id),
    node_name TEXT,
    preset_name TEXT,
    model_name TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now')),
    finished_at TEXT,
    duration_ms INTEGER,
    running INTEGER NOT NULL DEFAULT 0,
    output TEXT,
    response_json TEXT,
    success INTEGER NOT NULL DEFAULT 0
);

-- ansible_actions: structured ansible playbook action log
CREATE TABLE IF NOT EXISTS "ansible_actions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL CHECK(action_type IN (
        'validate_node','discover_node','deploy_instance','undeploy_instance',
        'restart_instance','stop_instance','get_logs','scan_models',
        'apt_update','apt_upgrade','reboot_node','shutdown_node',
        'force_delete','ansible_execute','update_build','update_and_compile',
        'rpc_health_check','execute_instance','config_change','preset_change'
    )),
    node_id INTEGER,
    instance_id INTEGER,
    actor TEXT DEFAULT 'system',
    status TEXT DEFAULT 'received',
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    started_at TEXT,
    finished_at TEXT,
    stdout TEXT,
    stderr TEXT,
    results_json TEXT,
    duration_ms INTEGER DEFAULT 0,
    playbook_name TEXT,
    task_summary TEXT,
   playbook_registry_id INTEGER,
    playbook_version TEXT,
    host TEXT,
    CONSTRAINT fk_ansible_node FOREIGN KEY (node_id) REFERENCES nodes(id),
    CONSTRAINT fk_ansible_instance FOREIGN KEY (instance_id) REFERENCES instances(id),
    CONSTRAINT fk_ansible_playbook FOREIGN KEY (playbook_registry_id) REFERENCES playbook_registry(id)
);

-- qr_actions: general action audit trail (v0.06+ extended for running task tracking)
CREATE TABLE IF NOT EXISTS "qr_actions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    node_id INTEGER,
    instance_id INTEGER,
    actor TEXT DEFAULT 'api',
    details TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    override INTEGER NOT NULL DEFAULT 0,
    status TEXT DEFAULT 'running' CHECK(status IN ('running','completed','failed','timeout','stuck')),
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    playbook_registry_id INTEGER
);

-- instance_logs: per-instance action log (append-only)
CREATE TABLE IF NOT EXISTS "instance_logs" (
    id INTEGER PRIMARY KEY,
    instance_id INTEGER REFERENCES instances(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK(action IN ('create','start','stop','restart','config_change','deploy','undeploy','health_check','state_transition','error','async_build','preflight','timeout','uuid_check','update_and_compile','update_build','force_delete')),
    status TEXT DEFAULT 'received',
    detail TEXT,
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- groups: instance grouping for batch operations
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    strategy TEXT DEFAULT 'round_robin' CHECK(strategy IN ('round_robin','weighted','health_first')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- group_members: instances and nodes within groups
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    instance_id INTEGER REFERENCES instances(id) ON DELETE CASCADE,
    node_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    weight INTEGER DEFAULT 1 CHECK(weight > 0),
    UNIQUE(group_id, instance_id),
    UNIQUE(group_id, node_id)
);

-- playbook_registry: tracked ansible playbooks with checksums
CREATE TABLE IF NOT EXISTS playbook_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    version TEXT DEFAULT '1',
    checksum_sha256 TEXT NOT NULL,
    file_type TEXT DEFAULT 'core' CHECK(file_type IN ('core', 'custom')),
    tags TEXT DEFAULT '',
    playbook_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    usage_counter_since_update INTEGER DEFAULT 0,
    error_counter_since_update INTEGER DEFAULT 0,
    file_size INTEGER DEFAULT NULL
);


-- request_log: API request audit log
CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER,
    duration_ms REAL,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- config_global: general key-value configuration store
CREATE TABLE IF NOT EXISTS config_global (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    description TEXT DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- node_configs: per-node, per-engine configuration overrides
CREATE TABLE IF NOT EXISTS node_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(node_id, engine_type_id, key)
);

-- applied_migrations: migration tracking (always exists, even in fresh DB)
CREATE TABLE IF NOT EXISTS applied_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- Incrementals (001, 002, 005) integrated into this base for v0.06.
-- Migration 003 (rpc→llama_rpc rename) kept as separate data-only migration for existing DB upgrades.

-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_benchmark_results_instance ON benchmark_results(instance_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_results_run ON benchmark_results(run_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_results_time ON benchmark_results(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_configs_engine ON engine_configs(engine_type_id);
CREATE INDEX IF NOT EXISTS idx_engine_types_enabled ON engine_types(enabled);
CREATE INDEX IF NOT EXISTS idx_instances_engine ON instances(engine_type_id);
CREATE INDEX IF NOT EXISTS idx_instances_name ON instances(name);
CREATE INDEX IF NOT EXISTS idx_instances_node ON instances(node_id);
CREATE INDEX IF NOT EXISTS idx_instances_port ON instances(port_assigned);
CREATE INDEX IF NOT EXISTS idx_instances_state ON instances(state);
CREATE INDEX IF NOT EXISTS idx_members_group ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_models_engine ON engine_models(engine_type_id);
CREATE INDEX IF NOT EXISTS idx_models_host ON engine_models(host_id);
CREATE INDEX IF NOT EXISTS idx_node_configs_engine ON node_configs(engine_type_id);
CREATE INDEX IF NOT EXISTS idx_node_configs_node ON node_configs(node_id);
CREATE INDEX IF NOT EXISTS idx_nodes_is_active ON nodes(is_active);
CREATE INDEX IF NOT EXISTS idx_nodes_ping_state ON nodes(ping_state);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_playbook_file_type ON playbook_registry(file_type);
CREATE INDEX IF NOT EXISTS idx_playbook_tags ON playbook_registry(tags);
CREATE INDEX IF NOT EXISTS idx_presets_engine ON engine_presets(engine_type_id);
CREATE INDEX IF NOT EXISTS idx_qr_actions_action_type ON qr_actions(action_type);
CREATE INDEX IF NOT EXISTS idx_qr_actions_created_at ON qr_actions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qr_actions_instance_id ON qr_actions(instance_id);
CREATE INDEX IF NOT EXISTS idx_qr_actions_node_id ON qr_actions(node_id);
CREATE INDEX IF NOT EXISTS idx_request_log_time ON request_log(created_at DESC);

PRAGMA foreign_keys = ON;
