"""Dependency check — pre-flight before any imports.

Reads requirements.txt, tries to import each package, reports missing ones
with human-readable install instructions. Called early in startup pipeline
to avoid cryptic ModuleNotFoundError tracebacks.

Usage:
    from lib.lib_deps import check_dependencies
    missing = check_dependencies()  # exits 1 if critical deps missing
"""

import os
import re
import sys


# pip package name → Python import name mapping.
# Some packages have different import names than their pip install names.
# Key = lowercase pip name, Value = module name to __import__().
_IMPORT_NAME_MAP = {
    "flask": "flask",
    "jinja2": "jinja2",
    "markupsafe": "markupsafe",
    "pyyaml": "yaml",          # PyYAML pip → yaml import
    "ansible": "ansible",
    "ansible-core": "ansible",  # ansible-core is part of ansible package
    "psutil": "psutil",
    "waitress": "waitress",
    "requests": "requests",
    "requests_ntlm": "requests_ntlm",
    "flask-cors": "flask_cors",  # flask-cors pip → flask_cors import
    "flask-socketio": "flask_socketio",  # Flask-SocketIO pip → flask_socketio import
    "python-dotenv": "dotenv",  # python-dotenv pip → dotenv import
}


def _parse_requirements(req_path):
    """Parse requirements.txt and return list of (import_name, package_name).

    Skips comments, blank lines, and options (-r, -e, etc.).
    Strips version specifiers (>=, ==, ~=) to get bare package names.
    Uses _IMPORT_NAME_MAP for packages with different import vs pip names.
    """
    packages = []
    if not os.path.isfile(req_path):
        return packages

    with open(req_path) as f:
        for line in f:
            line = line.strip()
            # Skip comments, blank lines, options
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Strip version specifiers: Flask>=3.1.3 → Flask
            pkg = re.split(r"[><=!~\[]", line)[0].strip()
            if not pkg:
                continue
            # Look up import name from pip package name
            imp_name = _IMPORT_NAME_MAP.get(pkg.lower())
            if imp_name is None:
                # Default: replace hyphens with underscores, lowercase
                imp_name = pkg.replace("-", "_").lower()
            packages.append((imp_name, pkg))

    return packages


def check_dependencies(requirements_file=None):
    """Check that all required Python dependencies are available.

    Reads requirements.txt and tries to import each package.
    Exits with code 1 and a friendly message if critical packages are missing.

    Args:
        requirements_file: Path to requirements.txt. Defaults to
            project root / requirements.txt relative to this module.

    Returns:
        list[str]: Names of missing packages (empty = all OK).
    """
    if requirements_file is None:
        # Project root is grandparent of lib/ dir
        _lib_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.dirname(_lib_dir)
        requirements_file = os.path.join(_project_root, "requirements.txt")

    packages = _parse_requirements(requirements_file)
    missing = []
    for imp_name, pkg_name in packages:
        try:
            __import__(imp_name)
        except ImportError:
            missing.append(pkg_name)

    if not missing:
        return []

    # Determine project root for path references
    _lib_dir = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.dirname(_lib_dir)

    # Print friendly error message
    print("[qr] DEPENDENCY CHECK FAILED — missing packages:", file=sys.stderr)
    for pkg in missing:
        # Determine apt package name from pip name
        apt_name = _pip_to_apt(pkg)
        if apt_name:
            print(
                f"  {pkg:20s} (required) — install: pip3 install {pkg} "
                f"or sudo apt install {apt_name}",
                file=sys.stderr,
            )
        else:
            print(f"  {pkg:20s} (required) — install: pip3 install {pkg}",
                  file=sys.stderr)

    print(f"\n[qr] See {_project_root}/requirements.txt for the full dependency list.",
          file=sys.stderr)
    print(
        "[qr] Hint: sudo apt update && sudo apt install -y "
        "python3-flask python3-jinja2 python3-markupsafe python3-ansible-core "
        "python3-pyyaml python3-psutil python3-requests "
        "python3-requests-ntlm python3-flask-cors python3-flask-socketio",
        file=sys.stderr,
    )

    sys.exit(1)
    return missing  # unreachable — but satisfies linter


def _pip_to_apt(pkg_name):
    """Convert pip package name to Debian apt package name.

    Returns the apt package name if one exists, None otherwise.
    Handles common mappings: Flask → python3-flask, PyYAML → python3-pyyaml, etc.
    """
    # Common pip→apt mappings (case-insensitive lookup)
    _PIP_TO_APT = {
        "flask": "python3-flask",
        "jinja2": "python3-jinja2",
        "markupsafe": "python3-markupsafe",
        "pyyaml": "python3-pyyaml",
        "ansible": "python3-ansible",
        "ansible-core": "python3-ansible-core",
        "psutil": "python3-psutil",
        "requests": "python3-requests",
        "requests_ntlm": "python3-requests-ntlm",
        "flask-cors": "python3-flask-cors",
        "flask-socketio": "python3-flask-socketio",
        "waitress": "python3-waitress",
    }

    # Try exact match (lowercase)
    key = pkg_name.lower()
    if key in _PIP_TO_APT:
        return _PIP_TO_APT[key]

    # Try common normalization: strip hyphens, capitalize first letter of each part
    normalized = "python3-" + "-".join(
        p.capitalize() for p in re.split(r"[-_]+", key)
    )
    return normalized if normalized in _PIP_TO_APT.values() else None


if __name__ == "__main__":
    # Standalone test: run dependency check
    check_dependencies()
