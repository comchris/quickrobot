"""quickrobot (v0.04) — Database module.

Exports all adapter functions for direct import from db package.
"""

from db.sqlite import get_connection, close_connection
from db.migration import (
    run_migrations, get_applied_migrations, apply_base_schema,
)

__all__ = [
    "get_connection",
    "close_connection",
    "run_migrations",
    "get_applied_migrations",
    "apply_base_schema",
]
