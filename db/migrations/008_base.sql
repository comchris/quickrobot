-- db/migrations/008_base.sql
-- Consolidated base schema for quickrobot v0.08 (unified logging + health checks).
-- Created: 2026-07-10 by Design Agent
-- Replaces: 007_base.sql + 008_unified_logs.sql + 008b_bg_health_checks.sql
-- Changes from 007:
--   - Unified logging: log_entries replaces (jobs, tasks, ansible_actions, qr_actions, instance_logs)
--   - BG health checks: scheduler_config table + health_check_enabled column on instances

PRAGMA foreign_keys = OFF;

-- =============================================================================
-- CORE TABLES (no dependencies)
-- =============================================================================

CREATE TABLE engine_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    module_path TEXT NOT NULL,
    version TEXT DEFAULT '1.0',
    enabled INTEGER DEFAULT 1 CHECK(enabled IN (0,1)),
    capabilities TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    display_order INTEGER DEFAULT 50,
    category TEXT DEFAULT 'utility'
);

CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    hostname TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'ansible' CHECK(transport IN ('ansible','ssh')),
    ansible_inventory_host TEXT,
    ansible_user TEXT,
    ssh_port INTEGER NOT NULL DEFAULT 22,
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
    ping_state TEXT DEFAULT 'disabled' CHECK(ping_state IN ('online', 'offline', 'disabled')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    ipv4_address TEXT,
    ipv6_address TEXT,
    model_base_path TEXT DEFAULT '/mnt/llama/gguf/models',
    node_build_state TEXT DEFAULT 'idle',
    host_type TEXT DEFAULT '' CHECK(host_type IN ('', 'baremetal', 'docker', 'lxc', 'vm'))
);

CREATE TABLE benchmark_prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE CHECK(LENGTH(name) > 0),
    content TEXT NOT NULL CHECK(LENGTH(content) > 0),
    max_tokens INTEGER NOT NULL DEFAULT 20,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    strategy TEXT DEFAULT 'round_robin' CHECK(strategy IN ('round_robin','weighted','health_first')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE playbook_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    version TEXT DEFAULT '1',
    checksum_sha256 TEXT NOT NULL,
    file_type TEXT DEFAULT 'core' CHECK(file_type IN ('core', 'custom', 'template')),
    tags TEXT DEFAULT '',
    playbook_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    usage_counter_since_update INTEGER DEFAULT 0,
    error_counter_since_update INTEGER DEFAULT 0,
    file_size INTEGER DEFAULT NULL
);

-- =============================================================================
-- TABLES WITH FK TO CORE
-- =============================================================================

CREATE TABLE engine_presets (
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

CREATE TABLE engine_models (
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

CREATE TABLE engine_configs (
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

CREATE TABLE node_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id),
    engine_type_id INTEGER NOT NULL REFERENCES engine_types(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(node_id, engine_type_id, key)
);

CREATE TABLE config_global (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    description TEXT DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE applied_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- =============================================================================
-- CORE LOGGING / AUDIT TABLES
-- =============================================================================

CREATE TABLE request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER,
    duration_ms REAL,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- =============================================================================
-- INSTANCE & DEPLOYMENT TABLES
-- =============================================================================

CREATE TABLE instances (
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
    instance_uuid TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(6)))),
    build_number TEXT,
    start_after_deploy INTEGER DEFAULT 0 CHECK(start_after_deploy IN (0,1)),
    rpc_bind_ids TEXT DEFAULT '[]' CHECK(rpc_bind_ids IS NULL OR json_valid(rpc_bind_ids)),
    split_mode TEXT DEFAULT 'layer' CHECK(split_mode IN ('layer','row','tensor',NULL)),
    tensor_split TEXT,
    split INTEGER DEFAULT 0,
    node_name TEXT,
    node_hostname TEXT,
    node_build_state TEXT DEFAULT 'idle',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    start_on_boot TEXT DEFAULT 'true' CHECK(start_on_boot IN ('true','false')),
    experts INTEGER DEFAULT 0 CHECK(experts >= 0 AND experts <= 1000),
    draft INTEGER DEFAULT 0 CHECK(draft >= 0 AND draft <= 1000),
    cli_flags TEXT DEFAULT '[]',
    health_check_enabled INTEGER DEFAULT 1 CHECK(health_check_enabled IN (0, 1)),
    UNIQUE(name, node_id)
);

CREATE TABLE group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    instance_id INTEGER REFERENCES instances(id) ON DELETE CASCADE,
    node_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    weight INTEGER DEFAULT 1 CHECK(weight > 0),
    UNIQUE(group_id, instance_id),
    UNIQUE(group_id, node_id)
);

CREATE TABLE benchmark_results (
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

CREATE TABLE config_levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 7),
    source TEXT NOT NULL,
    env_vars TEXT DEFAULT '{}',
    cli_opts TEXT DEFAULT '[]',
    model_params TEXT DEFAULT '{}',
    metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(instance_id, level)
);

-- =============================================================================
-- ENGINE JOB TYPE REGISTRY
-- =============================================================================

CREATE TABLE engine_job_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engine_type_name TEXT NOT NULL,
    job_type TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    requires_instance INTEGER DEFAULT 1 CHECK(requires_instance IN (0, 1)),
    max_concurrent INTEGER DEFAULT 1 CHECK(max_concurrent > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- =============================================================================
-- UNIFIED LOGGING (log_entries) — replaces jobs/tasks/ansible_actions/qr_actions/instance_logs
-- =============================================================================

CREATE TABLE log_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Hierarchy: NULL = job-header row, set for task sub-rows
    parent_id           INTEGER REFERENCES log_entries(id),
    
    -- Identity
    job_type            TEXT,
    engine_type_name    TEXT,
    
    -- Target references
    instance_id         INTEGER REFERENCES instances(id),
    node_id             INTEGER REFERENCES nodes(id),
    
    -- Execution state
    status              TEXT NOT NULL DEFAULT 'running',
    actor               TEXT DEFAULT 'api',
    error_message       TEXT,
    
    -- Timing
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    duration_ms         INTEGER DEFAULT 0,
    
    -- Task-level detail (NULL for job-header rows)
    task_stage          TEXT,
    stage_playbook      TEXT,
    retry_count         INTEGER DEFAULT 0,
    max_retries         INTEGER DEFAULT 1,
    
    -- Rich output
    details_json        TEXT,
    results_json        TEXT,
    
    -- Audit
    playbook_registry_id INTEGER,
    playbook_version    TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_parent ON log_entries(parent_id);
CREATE INDEX IF NOT EXISTS idx_log_status ON log_entries(status);
CREATE INDEX IF NOT EXISTS idx_log_created ON log_entries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_instance ON log_entries(instance_id);

-- =============================================================================
-- SCHEDULED HEALTH CHECKS — config store
-- =============================================================================

CREATE TABLE scheduler_config (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL DEFAULT '',
    description   TEXT
);

-- =============================================================================
-- SCRIPT EXECUTION (dynamic multi-step scripts)
-- =============================================================================

CREATE TABLE scripts (
    id INTEGER PRIMARY KEY,
    parent_job_id INTEGER REFERENCES log_entries(id) ON DELETE CASCADE,
    actor TEXT DEFAULT 'api',
    name TEXT,
    status TEXT DEFAULT 'queued' CHECK(status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    total_steps INTEGER DEFAULT 0,
    completed_steps INTEGER DEFAULT 0,
    failed_steps INTEGER DEFAULT 0,
    skipped_steps INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE script_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id INTEGER NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    type TEXT NOT NULL,
    params TEXT DEFAULT '{}',
    depends_on TEXT DEFAULT '[]',
    if_condition TEXT,
    status TEXT DEFAULT 'queued' CHECK(status IN ('queued', 'running', 'completed', 'failed', 'skipped')),
    result TEXT DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE playbook_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    ansible_action_id INTEGER,
    output TEXT,
    timing_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

-- =============================================================================
-- INDEXES
-- =============================================================================

CREATE INDEX idx_benchmark_results_instance ON benchmark_results(instance_id);
CREATE INDEX idx_benchmark_results_run ON benchmark_results(run_id);
CREATE INDEX idx_benchmark_results_time ON benchmark_results(finished_at DESC);
CREATE INDEX idx_configs_engine ON engine_configs(engine_type_id);
CREATE INDEX idx_engine_types_enabled ON engine_types(enabled);
CREATE INDEX idx_instances_engine ON instances(engine_type_id);
CREATE INDEX idx_instances_name ON instances(name);
CREATE INDEX idx_instances_node ON instances(node_id);
CREATE INDEX idx_instances_port ON instances(port_assigned);
CREATE INDEX idx_instances_state ON instances(state);
CREATE INDEX idx_instances_health_check ON instances(health_check_enabled);
CREATE INDEX idx_members_group ON group_members(group_id);
CREATE INDEX idx_models_engine ON engine_models(engine_type_id);
CREATE INDEX idx_models_host ON engine_models(host_id);
CREATE INDEX idx_node_configs_engine ON node_configs(engine_type_id);
CREATE INDEX idx_node_configs_node ON node_configs(node_id);
CREATE INDEX idx_nodes_is_active ON nodes(is_active);
CREATE INDEX idx_nodes_ping_state ON nodes(ping_state);
CREATE INDEX idx_nodes_status ON nodes(status);
CREATE INDEX idx_playbook_file_type ON playbook_registry(file_type);
CREATE INDEX idx_playbook_tags ON playbook_registry(tags);
CREATE INDEX idx_presets_engine ON engine_presets(engine_type_id);
CREATE INDEX idx_request_log_time ON request_log(created_at DESC);
CREATE UNIQUE INDEX idx_job_type_engine ON engine_job_types(engine_type_name, job_type);
CREATE INDEX idx_ejt_engine ON engine_job_types(engine_type_name);
CREATE INDEX idx_script_steps_script ON script_steps(script_id);
CREATE INDEX idx_config_levels_instance ON config_levels(instance_id);
CREATE INDEX idx_config_levels_level ON config_levels(level);
CREATE INDEX idx_nodes_hostname ON nodes(hostname);
CREATE INDEX idx_playbook_runs_task ON playbook_runs(task_id);
