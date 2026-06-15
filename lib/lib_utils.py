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

"""Quickrobot — Common utility functions.

Functions: relative_age — convert ISO timestamp to relative age string.
"""


def relative_age(iso_timestamp):
    """Convert an ISO 8601 timestamp to a relative age string.

    Args:
        iso_timestamp: ISO 8601 datetime string (e.g., '2026-05-15T14:30:00').

    Returns:
        Relative age string like '2h ago', '3d ago', or 'just now'.
        Returns 'unknown' if timestamp is invalid.
    """
    from datetime import datetime as _dt, timezone as _tz

    try:
        # Parse ISO timestamp — timestamps are stored in UTC, parse as naive UTC
        ts_str = iso_timestamp or ""
        if not ts_str or ts_str == "None":
            return "unknown"
        # Handle various ISO formats (strip timezone offset before parsing)
        # SQLite stores with space separator, standard ISO uses 'T'
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                created = _dt.strptime(ts_str[:19], fmt)  # Strip trailing offset if present
                break
            except ValueError:
                continue
        else:
            return "unknown"

        # Compare against UTC now (timestamps in DB are UTC)
        now = _dt.now(_tz.utc).replace(tzinfo=None)
        diff = now - created
        total_seconds = int(diff.total_seconds())

        if total_seconds < 60:
            return "just now"
        elif total_seconds < 3600:
            mins = total_seconds // 60
            return f"{mins}m ago"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours}h ago"
        elif total_seconds < 604800:
            days = total_seconds // 86400
            return f"{days}d ago"
        else:
            weeks = total_seconds // 604800
            return f"{weeks}w ago"
    except Exception:
        return "unknown"
