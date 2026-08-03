"""PID file path utility.

Shared across quickrobot.py and qr_api/__init__.py to construct
PID file paths from a database directory path.
"""

import os


def get_pid_name(name="quickrobot"):
    """Return the filename component of a PID file."""
    return f"qr_{name}.pid"


def get_pid_path(db_dir, name="quickrobot"):
    """Construct full PID file path. Caller supplies db_dir."""
    if not db_dir:
        db_dir = "data"
    return os.path.join(db_dir, get_pid_name(name))
