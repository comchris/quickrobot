"""Platform utility functions.

Shared across engine entry points for root guard and other
OS-level checks that need to work on both POSIX and Windows.
"""

import os
import sys


def check_nonroot():
    """Refuse to run as root on POSIX systems. No-op on Windows."""
    try:
        if os.getuid() == 0:
            print("this robot won't run as root", file=sys.stderr)
            sys.exit(1)
    except AttributeError:
        pass  # Windows — uid not applicable
