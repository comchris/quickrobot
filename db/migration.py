# Copyright 2026 comchris quickrobot .de project 
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""quickrobot (v0.07) — Migration runner with idempotent tracking.

Base schema (007_base.sql) is the definitive schema definition — always
applied on startup, idempotent via CREATE TABLE IF NOT EXISTS.

Incremental migration files are tracked in applied_migrations table and
only run when first encountered. No wildcard glob: filenames are explicit
constants, not discovered at runtime.
"""

import os


# Explicit schema and migration file names — no wildcard discovery
BASE_SCHEMA_FILE = "007_base.sql"

# Incremental migration files (applied only once, tracked in DB)
# Add entries here when schema changes require migration.
INCREMENTAL_MIGRATIONS = [
    # All previous migrations (008–013) consolidated into 007_base.sql
    # Add new entries here when future schema changes require migration.
]


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


def apply_base_schema(db_path, schema_file=None):
    """Apply the base schema to a fresh database.

    Reads the explicit base schema file and executes it. Idempotent —
    CREATE TABLE IF NOT EXISTS means repeated application is safe.

    Args:
        db_path: Path to the SQLite database file.
        schema_file: Optional override for the base schema filename.
            Defaults to BASE_SCHEMA_FILE constant.
    """
    if schema_file is None:
        schema_file = BASE_SCHEMA_FILE

    from db.sqlite import get_connection as _get_conn

    conn = _get_conn(db_path)
    try:
        schema_path = os.path.join(
            os.path.dirname(__file__), "migrations", schema_file
        )
        with open(schema_path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        conn.executescript(sql)
        conn.commit()
    except Exception as exc:
        from db.sqlite import close_connection
        close_connection(conn)
        raise RuntimeError(f"Base schema failed: {exc}") from exc


def run_migrations(db_path, migrations_dir="db/migrations"):
    """Execute incremental migration files against the database.

    Always runs — no mode branching. Only checks INCREMENTAL_MIGRATIONS
    (explicit list), not all SQL files. Prints count of applied migrations.

    Args:
        db_path: Path to the SQLite database file.
        migrations_dir: Directory containing migration SQL files.

    Returns:
        int — number of migrations applied.
    """
    from db.sqlite import get_connection as _get_conn, close_connection

    conn = _get_conn(db_path)

    # Check which incremental migrations are already recorded
    try:
        applied = get_applied_migrations(conn)
    except Exception:
        applied = set()

    applied_count = 0

    for migration_file in INCREMENTAL_MIGRATIONS:
        if migration_file in applied:
            continue

        migration_path = os.path.join(migrations_dir, migration_file)
        try:
            with open(migration_path, "r", encoding="utf-8") as fh:
                sql = fh.read()

            conn.executescript(sql)
            _ensure_migration_table(conn)
            conn.execute(
                "INSERT INTO applied_migrations (name) VALUES (?)", (migration_file,)
            )
            conn.commit()
            applied.add(migration_file)
            applied_count += 1
        except Exception as exc:
            exc_msg = str(exc)
            # Treat idempotent errors as success
            if "duplicate column" in exc_msg or "already exists" in exc_msg:
                _ensure_migration_table(conn)
                conn.execute(
                    "INSERT INTO applied_migrations (name) VALUES (?)", (migration_file,)
                )
                conn.commit()
                applied.add(migration_file)
                applied_count += 1
            else:
                close_connection(conn)
                raise RuntimeError(f"Migration {migration_file} failed: {exc}") from exc

    close_connection(conn)
    return applied_count
