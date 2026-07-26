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

"""Quickrobot — Shared state machine helper for engine classes.

All engines start from VALID_TRANSITIONS (base) in db.adapters.instances,
then apply their class-level STATE_EXTENSIONS dict to add/modify transitions.
This eliminates duplicated get_state_machine() code across all 8 engine files.

Usage:
    class MyEngine(BaseEngine):
        STATE_EXTENSIONS = {
            "running": ["configuring"],
            ...
        }
"""

# All valid instance state names — used by routes for guards, transitions, and counts.
# Single source of truth for instance state string literals.
VALID_INSTANCE_STATES = frozenset([
    "unconfigured", "configuring", "deploying", "starting", "loading",
    "running", "stopping", "stopped", "error", "build_error",
    "deployed", "updating", "compiling", "timeout",
])

# States allowed for undeploy operations (excludes unconfigured)
VALID_UNDEPLOY_STATES = VALID_INSTANCE_STATES - frozenset(["unconfigured"])

# States requiring active health checking (excludes terminal/stable states:
# unconfigured=never started, deployed=idle, stopping=in-transition)
HEALTH_CHECK_STATES = VALID_INSTANCE_STATES - frozenset([
    "unconfigured", "deployed", "stopping",
])

# Operation-specific state subsets — used by route handlers as guard conditions.
# Each constant captures a semantically meaningful subset of VALID_INSTANCE_STATES
# that represents allowed states for a specific operation or condition.

# Start: states where idempotent start returns immediately without remote probe
IDEMPOTENT_START_STATES = frozenset(["running", "starting"])

# Auto-deploy trigger states — start() auto-deploys if instance is in these states
AUTO_DEPLOY_STATES = frozenset(["unconfigured", "deploying"])

# Restart: states that trigger restart-from-nonrunning logging
RESTART_FROM_NONRUNNING = frozenset(["deployed", "stopped"])

# Subprocess engine: states requiring proper stop->start cycle (not skip)
SUBPROCESS_CYCLE_STATES = frozenset(["running", "stopping"])

# Undeploy: transitional states that need force-stopping before undeploy
UNDEPLOY_TRANSITIONAL_STATES = frozenset(["starting", "stopping", "deploying"])

# Stop operation: states from which stop is allowed (excludes unconfigured,
# stopped, configuring, compiling, updating, timeout — not valid to stop)
STOP_ALLOWED_STATES = frozenset([
    "running", "starting", "stopping", "deployed", "error", "build_error", "loading",
])

# Reconfigure: states from which config change is allowed (stable/idle states only)
RECONFIGURE_ALLOWED_STATES = frozenset(["running", "stopped", "error", "deployed"])


def build_state_machine(extensions=None, removals=None):
    """Build a state machine from base transitions + engine modifications.

    Args:
        extensions: Optional dict mapping state -> list of additional next states.
                    Each entry is merged into the existing transitions for that state.
        removals: Optional list of state names to remove from ALL state entries.
                  Useful for engines that don't have build/update states.

    Returns:
        dict: Complete state machine (current_state -> [allowed_next_states]).
    """
    from db.adapters.instances import VALID_TRANSITIONS  # lazy import — avoids circular dep
    sm = dict(VALID_TRANSITIONS)

    # Apply removals first (remove states from all entries)
    if removals:
        for key in list(sm.keys()):
            sm[key] = [s for s in sm[key] if s not in removals]

    # Then apply extensions (add states to specific entries)
    if extensions is None:
        return sm

    for state, additions in extensions.items():
        if state in sm:
            existing = set(sm[state])
            existing.update(additions)
            sm[state] = list(existing)
        else:
            sm[state] = list(additions)

    return sm
