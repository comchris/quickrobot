#!/usr/bin/env python3
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
"""quickrobot -- Timezone-aware timestamp formatting utilities.

All timestamps in the database are stored as UTC (naive ISO strings without
timezone suffix). Display formatting converts these to the configured local
timezone for user presentation.

Functions:
    utcnow_str          Current UTC timestamp as naive ISO string
    get_local_tz_name   IANA timezone name of the current OS process
    parse_tz_offset     Parse IANA TZ name to UTC offset in hours
    format_utc_to_display  Convert UTC ISO string to localized display
    relative_age_local  Relative age computed with tz awareness
"""

import time
from datetime import datetime, timezone


try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # Will handle gracefully in functions


# Default fallback offset when TZ name is unrecognised (CET)
_DEFAULT_OFFSET_HOURS = 1.0


def utcnow_str():
    """Return current UTC timestamp as an ISO string with Z suffix.

    Format: 'YYYY-MM-DDTHH:MM:SSZ' (trailing Z indicates UTC).
    This is the canonical way to get a UTC timestamp string for DB storage.
    The Z suffix ensures JavaScript's new Date() always parses as UTC,
    not local time.

    Returns:
        str: UTC timestamp in ISO format with 'Z' suffix.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_local_tz_name():
    """Return the IANA timezone name of the current OS process.

    Reads from /etc/timezone first, falls back to time.tzname[0],
    then falls back to "UTC".

    Returns:
        str: IANA timezone name (e.g. "Europe/Berlin", "UTC").
    """
    try:
        with open("/etc/timezone", "r") as f:
            tz = f.read().strip()
            if tz and ZoneInfo is not None:
                try:
                    ZoneInfo(tz)
                    return tz
                except (KeyError, Exception):
                    pass
    except (OSError, IOError):
        pass

    # Fallback 1: time.tzname[0] -- often a 3-letter abbreviation
    tz = time.tzname[0]
    if tz and tz != "UTC":
        return tz

    return "UTC"


def parse_tz_offset(tz_name="Europe/Berlin"):
    """Parse an IANA timezone name and return its UTC offset in hours.

    Uses Python's zoneinfo module to compute the offset for the current
    moment (accounts for DST transitions).

    Args:
        tz_name: IANA timezone name string (e.g. "America/New_York").

    Returns:
        float: UTC offset in hours (e.g. +2.0 for CEST, -5.0 for EST).
        Falls back to _DEFAULT_OFFSET_HOURS if the TZ name is invalid.
    """
    if ZoneInfo is None:
        return _DEFAULT_OFFSET_HOURS

    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        offset = now.utcoffset().total_seconds() / 3600.0
        return round(offset, 1)
    except (KeyError, Exception):
        return _DEFAULT_OFFSET_HOURS


def format_utc_to_display(utc_iso, tz_name="Europe/Berlin"):
    """Convert a UTC ISO string to a localized display string.

    Parses the UTC timestamp and formats it in the target timezone.
    Handles both formats: with Z suffix (e.g. '2026-05-28T14:00:00Z')
    and without (e.g. '2026-05-28T14:00:00') for backward compatibility.

    Args:
        utc_iso: UTC ISO string (e.g. '2026-05-28T14:00:00').
        tz_name: IANA timezone name for display (default: Europe/Berlin).

    Returns:
        str: Localized datetime string ('YYYY-MM-DD HH:MM:SS'),
             or the original input if parsing fails.
    """
    try:
        # Strip trailing Z if present for parsing
        clean = utc_iso.rstrip("Z") if utc_iso else ""
        dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        if ZoneInfo is None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        local_dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return utc_iso or "-"


def relative_age_local(utc_iso, tz_name="Europe/Berlin"):
    """Compute relative age using timezone-aware "now".

    Same logic as lib_utils.relative_age() but ensures the reference
    point is timezone-aware for correctness. The difference in seconds
    is timezone-independent, so this produces the same result as the
    naive implementation when both timestamps are UTC.
    Handles both formats: with Z suffix and without for backward compat.

    Args:
        utc_iso: UTC ISO string (e.g. '2026-05-28T14:00:00').
        tz_name: IANA timezone name (used for now reference).

    Returns:
        str: Relative age string (e.g. '10d 14h', '23h 30m', '55s'),
             or '-' if the input is invalid.
    """
    if not utc_iso or utc_iso == "-":
        return "-"
    try:
        # Strip trailing Z if present for parsing
        clean = utc_iso.rstrip("Z") if utc_iso else ""
        dt = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 0:
            return utc_iso
        d = diff // 86400
        diff %= 86400
        h = diff // 3600
        diff %= 3600
        m = diff // 60
        s = diff % 60
        if d >= 7:
            return f"{d}d"
        if d > 0:
            return f"{d}d {h}h"
        if h > 0:
            return f"{h}h {m}m"
        if m > 0:
            return f"{m}m {s}s"
        return f"{s}s"
    except (ValueError, TypeError):
        return "-"
