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

"""Quickrobot — SQLite connection helper with WAL mode.

Provides a simple connection factory that ensures WAL journal mode
and a 5-second busy timeout on every connection.
"""

import os
import sqlite3


def get_connection(db_path):
    """Open a new SQLite connection with WAL mode and timeout configured.

    Creates the data directory if it does not exist yet.
    Sets PRAGMA journal_mode=WAL and busy_timeout=5000ms.
    Returns a standard sqlite3.Connection (row_factory left to caller).

    Args:
        db_path: Absolute or relative path to the SQLite database file.

    Returns:
        sqlite3.Connection configured with WAL and timeout settings.
    """
    dir_name = os.path.dirname(db_path)
    if dir_name and not os.path.isdir(dir_name):
        os.makedirs(dir_name, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def close_connection(conn):
    """Close a database connection safely.

    Args:
        conn: sqlite3.Connection to close.
    """
    if conn:
        try:
            conn.close()
        except Exception:
            pass


class _ConnectionPool:
    """Simple thread-local connection pool using context managers."""

    def __init__(self, db_path):
        self._db_path = db_path
        self._conn = None

    def __enter__(self):
        self._conn = get_connection(self._db_path)
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            if self._conn:
                self._conn.commit()
        else:
            if self._conn:
                self._conn.rollback()
        close_connection(self._conn)
        self._conn = None
        return False


def pool(db_path):
    """Return a context manager that yields a configured connection.

    Usage::
        with pool(db_path) as conn:
            cursor = conn.execute("SELECT ...")

    The connection is committed on success or rolled back on exception.
    """
    return _ConnectionPool(db_path)
