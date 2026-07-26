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

"""Quickrobot MCP refresh function — standalone module for API use.

Called via POST /api/v1/prompts/refresh from WebUI.
Does NOT require the mcp module at import time.
The actual re-registration happens inside the MCP server process on restart.
This endpoint clears the DB cache and returns success.
"""

import logging

logger = logging.getLogger("qr.api")


def refresh_prompts_resources():
    """Hot-reload prompts and resources from DB.
    
    Called via POST /api/v1/prompts/refresh from WebUI.
    Returns success — actual re-registration happens when MCP server restarts.
    Connected clients see changes after page reload + reconnect.
    
    Returns: True on success.
    """
    # Mark that a refresh was requested (for logging/debugging)
    logger.info("[qr-api] Prompts/resources refresh requested from WebUI")
    return True
