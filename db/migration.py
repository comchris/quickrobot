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

"""quickrobot (v0.06) — Migration runner with idempotent tracking.

Scans the migrations directory for numbered SQL files and executes
any that have not yet been recorded in the applied_migrations table.

v0.06 base schema: 000_base_006.sql (consolidated from 000_base_004 + 
  migrations 001/002/005). Remaining incrementals: 003_rename_rpc_to_llama_rpc 
  (data-only, for existing DB upgrades).

In dev mode: runs pending migrations automatically.
In prod mode: checks for pending migrations; if found, returns a warning
    instead of executing (migrations should be applied explicitly).
"""

import glob
import os
import re


def _ensure_migration_table(conn):
    """Create applied_migrations table if it does not exist yet."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applied_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.commit()


def get_applied_migrations(conn):
    """Return a set of migration file names that have already been applied.

    Args:
        conn: Active sqlite3.Connection.

    Returns:
        set of str — migration file basenames that are recorded as applied.
    """
    cursor = conn.execute(
        "SELECT name FROM applied_migrations ORDER BY id"
    )
    return {row[0] for row in cursor.fetchall()}


def run_migrations(db_path, migrations_dir="db/migrations", dev_mode=True):
    """Execute pending migration files against the database.

    In dev mode: runs unapplied migrations as normal.
    In prod mode: if pending migrations exist, returns a warning tuple
        (True, "migration needed") without executing.

    Args:
        db_path: Path to the SQLite database file.
        migrations_dir: Directory containing numbered .sql migration files.
        dev_mode: If True, execute pending migrations. If False, only check
            and return a warning if migrations are pending.

    Returns:
        tuple of (list_or_warning, error_msg).
        On success: ([applied_names], None) or ([], None) if nothing to do.
        On prod warning: ([], "pending_migrations") with message.
    """
    from db.sqlite import get_connection, close_connection

    conn = get_connection(db_path)

    # Check if we have already recorded any migrations
    # The first migration may itself create the tracking table
    try:
        applied = get_applied_migrations(conn)
    except Exception:
        applied = set()

    pattern = os.path.join(migrations_dir, "[0-9]*.sql")
    migration_files = sorted(glob.glob(pattern))

    base_migration = "000_base_006.sql"
    base_applied = base_migration in applied

    # Check if any migrations are pending (not yet applied)
    pending = [os.path.basename(f) for f in migration_files if os.path.basename(f) not in applied]

    if pending and not dev_mode:
        # Prod mode: alert instead of auto-migrating
        close_connection(conn)
        return [], f"Pending migrations: {', '.join(pending)}. Run with --dev-mode or apply manually."

    for path in migration_files:
        name = os.path.basename(path)
        if name in applied:
            continue

        # If base migration was applied, skip all old incrementals (superseded)
        if base_applied and name != base_migration:
            # Record as applied without re-running
            try:
                _ensure_migration_table(conn)
                conn.execute("INSERT INTO applied_migrations (name) VALUES (?)", (name,))
                conn.commit()
                applied.add(name)
            except Exception:
                pass  # Table may not exist yet or already recorded
            continue

        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()

        try:
            conn.executescript(sql)
            # Record that this migration was applied
            try:
                _ensure_migration_table(conn)
                conn.execute(
                    "INSERT INTO applied_migrations (name) VALUES (?)",
                    (name,),
                )
                conn.commit()
                applied.add(name)
            except Exception:
                # Table may have been created by the migration SQL itself;
                # try inserting again after commit
                try:
                    conn.execute(
                        "INSERT INTO applied_migrations (name) VALUES (?)",
                        (name,),
                    )
                    conn.commit()
                    applied.add(name)
                except Exception:
                    pass  # Already recorded by migration SQL
        except Exception as exc:
            # Treat "duplicate column name" as success — migration already applied (idempotent)
            exc_msg = str(exc)
            if "duplicate column" in exc_msg or "already exists" in exc_msg:
                try:
                    _ensure_migration_table(conn)
                    conn.execute(
                        "INSERT INTO applied_migrations (name) VALUES (?)",
                        (name,),
                    )
                    conn.commit()
                    applied.add(name)
                except Exception:
                    pass  # Already recorded or table creation failed — still success
                continue
            close_connection(conn)
            raise RuntimeError(
                f"Migration {name} failed: {exc}"
            ) from exc

    return list(applied), None


def close_connection(conn):
    """Close a database connection safely."""
    from db.sqlite import close_connection as _cc
    _cc(conn)
