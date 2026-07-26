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

"""Quickrobot MCP prompts — system prompts and context templates.

Registers MCP prompt functions that read from files on disk (prompts/*.md).
TYPE=MCP prompts are dynamically registered at each new MCP connection.
ROLE=skill prompts read from .opencode/skills/*/SKILL.md files.
All TYPE=MCP prompts use files as SSOT — no DB content fallback.

Usage:
    from engine.qr_mcp_prompts import register_prompts
    register_prompts(mcp_server)

Prompts exposed via MCP protocol:
    prompts/list  -> [designer-system-prompt + dynamic TYPE=MCP]
    prompts/get?name=designer-system-prompt -> GetPromptResult with content
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger("mcp")


def _get_project_root():
    """Resolve the project root directory relative to this file.

    The MCP server runs from `engine/` subdir, so parent chain is:
    engine → quickrobot → (project root)
    """
    return Path(__file__).resolve().parent.parent


def _get_prompt_content(prompt_id):
    """Read prompt from disk file — SSOT, no DB fallback.

    Args:
        prompt_id: Stable identifier of the prompt in prompts/ directory.

    Returns:
        str — prompt content from file, or None if not found.
    """
    project_root = _get_project_root()
    filepath = os.path.join(project_root, "prompts", f"{prompt_id}.md")
    
    try:
        if os.path.isfile(filepath):
            return Path(filepath).read_text(encoding="utf-8")
    except Exception as e:
        logger.debug("[mcp-prompt] File read failed for '%s': %s", prompt_id, e)

    return None


def _default_content(prompt_id):
    """Return embedded fallback content for a given prompt_id.

    Used only when the file is not found on disk.
    
    Args:
        prompt_id: The prompt identifier for which to provide a fallback.
    
    Returns:
        str — fallback content string.
    """
    defaults = {
        "project-overview-quick": "[Quick] No file found for project-overview-quick",
        "project-overview-full": "[Full] No file found for project-overview-full",
    }
    return defaults.get(prompt_id, f"[ERROR] Prompt '{prompt_id}' not found on disk")


def _verify_file_integrity(prompt_id):
    """Verify prompt file checksum and size match DB records.
    
    Both checksum AND size must match. If either differs, returns mismatch.
    
    Args:
        prompt_id: Stable identifier of the prompt in prompts/ directory.
    
    Returns:
        tuple — (intact: bool, reason: str)
          intact=True → file verified, serve normally
          intact=False → mismatch detected, block usage
    """
    project_root = _get_project_root()
    from db.adapters.prompts import get_prompt_by_id as _get_prompt
    
    filepath = project_root / "prompts" / f"{prompt_id}.md"
    
    if not filepath.is_file():
        return False, f"file checksum/size mismatch (FILE_MISSING: {filepath})"
    
    # Get stored checksum/size from DB
    row = _get_prompt(_get_db_path(), prompt_id)
    db_checksum = row.get("checksum_sha256", "") if row else ""
    db_size = row.get("file_size") if row else None
    
    # Check size first (faster than hashing)
    actual_size = filepath.stat().st_size
    if db_size and actual_size != db_size:
        return False, f"file checksum/size mismatch (size DB={db_size} disk={actual_size})"
    
    # Check checksum
    import hashlib
    actual_hash = hashlib.sha256(filepath.read_bytes()).hexdigest()
    if db_checksum and actual_hash != db_checksum:
        return False, f"file checksum/size mismatch (DB={db_checksum[:16]}... disk={actual_hash[:16]}...)"
    
    return True, "OK"


def _safe_read_skill(skill_name):
    """Read a skill file from .opencode/skills/<skill_name>/SKILL.md.

    Args:
        skill_name: Name of the skill (used as folder name).

    Returns:
        str — skill content, or None on failure.
    """
    project_root = _get_project_root()
    skill_dir = project_root / ".opencode" / "skills" / skill_name
    skill_file = skill_dir / "SKILL.md"
    
    if not skill_file.is_file():
        logger.warning("[mcp-prompt] Skill file missing: %s", skill_file)
        return None
    
    try:
        content = skill_file.read_text(encoding="utf-8")
        # Prepend metadata for context
        return (
            f"# Skill: {skill_name}\n"
            f"Source: .opencode/skills/{skill_name}/SKILL.md\n\n"
            f"{content}"
        )
    except Exception as e:
        logger.error("[mcp-prompt] Error reading skill %s: %s", skill_name, e)
        return None


def _get_db_path():
    """Resolve the database path without importing from qr_api.

    Tries multiple fallback strategies so the MCP subprocess (which runs
    in an isolated pipx venv without Flask) can still access the DB.

    Returns:
        str — absolute path to quickrobot.db, or empty string on failure.
    """
    # Strategy 1: .quickrobot.env env var
    env_path = os.getenv("QUICKROBOT_DB_PATH")
    if env_path:
        return Path(env_path).resolve().as_posix()

    # Strategy 2: CLI arg from qr_mcp_server.py startup block
    # (passed as --db by the scheduler, not MCP — but check anyway)
    try:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--db", default=None)
        args, _ = parser.parse_known_args()
        if args.db:
            return Path(args.db).resolve().as_posix()
    except Exception as _e:
        logger.debug("[mcp-prompt] DB path CLI arg parse failed: %s", _e)
        pass

    # Strategy 3: Default path relative to project root
    project_root = _get_project_root()
    db_candidate = project_root / "data" / "quickrobot.db"
    if db_candidate.is_file():
        return db_candidate.as_posix()

    # Strategy 4: Try from .env file
    env_file = project_root / ".quickrobot.env"
    if env_file.is_file():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("QUICKROBOT_DB_PATH="):
                    return Path(line.split("=", 1)[1]).resolve().as_posix()
        except Exception as _e:
            logger.debug("[mcp-prompt] .env DB path parse failed: %s", _e)
            pass

    return ""


def _register_dynamic_prompts(mcp):
    """Query DB for TYPE=MCP prompts and register them dynamically.

    Called at each new MCP connection to ensure latest DB state is available.
    
    Args:
        mcp: FastMCP server instance.
    """
    try:
        import json as _json
        from db.adapters.prompts import get_mcp_prompts
        # CRITICAL: Must use FastMCP's Prompt class (has .render method), NOT mcp.types.Prompt
        from mcp.server.fastmcp.prompts.base import Prompt, PromptArgument
        
        # Get db_path without importing qr_api (which requires Flask)
        db_path = _get_db_path()
        if not db_path:
            logger.warning("[mcp-prompt] DB path not found, skipping dynamic registration")
            return
        
        # Query for TYPE=MCP prompts
        mcp_prompts = get_mcp_prompts(db_path)
        
        if not mcp_prompts:
            logger.debug("[mcp-prompt] No TYPE=MCP prompts found in DB")
            return
        
        logger.info("[mcp-prompt] Registering %d TYPE=MCP prompts from DB", len(mcp_prompts))
        
        for prompt_data in mcp_prompts:
            prompt_id = prompt_data.get("prompt_id")
            title = prompt_data.get("title", prompt_id)
            description = prompt_data.get("description", "") or ""
            
            # Skip skill entries — they are registered as MCP Resources (not Prompts)
            # by _register_skill_resources(). message_role='skill' means resource, not prompt.
            if prompt_data.get("message_role") == "skill":
                logger.debug("[mcp-prompt] Skipping skill entry '%s' (registered as resource)", prompt_id)
                continue
            
            # Parse arguments from DB — stored as JSON string, e.g. '[]' or '[{"name":"depth",...}]'
            raw_args = prompt_data.get("arguments") or "[]"
            try:
                db_args = _json.loads(raw_args)
            except (_json.JSONDecodeError, TypeError):
                db_args = []
            
            # Build the content function with correct signature based on DB arguments
            # Wraps content retrieval with MCP usage/error counter tracking
            if not db_args:
                # No arguments — use a factory to capture prompt_id in outer scope
                # so inner function has 0 params (FastMCP introspection sees none)
                def _make(_pid=prompt_id):
                    async def _fn():
                        try:
                            intact, reason = _verify_file_integrity(_pid)
                            if not intact:
                                logger.warning("[mcp-prompt] BLOCKED %s: %s", _pid, reason)
                                from db.adapters.prompts import increment_prompt_error_counter
                                increment_prompt_error_counter(_get_db_path(), _pid)
                                return f"[CHECKSUM MISMATCH] {reason}. Verify and update checksum in edit screen."
                            c = _get_prompt_content(_pid)
                            if not c:
                                from db.adapters.prompts import increment_prompt_error_counter
                                increment_prompt_error_counter(_get_db_path(), _pid)
                                return f"[ERROR] Prompt '{_pid}' not found on disk"
                            from db.adapters.prompts import increment_prompt_usage_counter
                            increment_prompt_usage_counter(_get_db_path(), _pid)
                            return c
                        except Exception as e:
                            logger.error("[mcp-prompt] Invocation error for %s: %s", _pid, e)
                            raise
                    return _fn
                content_func = _make()
                # CRITICAL: Set __name__ so FastMCP uses prompt_id as internal key
                # (FastMCP uses func.__name__ to deduplicate, not the `name` param)
                content_func.__name__ = prompt_id
            else:
                # Has arguments — factory captures prompt_id, inner function uses **kwargs
                def _make(_pid=prompt_id):
                    async def _fn(**kwargs):
                        try:
                            intact, reason = _verify_file_integrity(_pid)
                            if not intact:
                                logger.warning("[mcp-prompt] BLOCKED %s: %s", _pid, reason)
                                from db.adapters.prompts import increment_prompt_error_counter
                                increment_prompt_error_counter(_get_db_path(), _pid)
                                return f"[CHECKSUM MISMATCH] {reason}. Verify and update checksum in edit screen."
                            c = _get_prompt_content(_pid)
                            if not c:
                                from db.adapters.prompts import increment_prompt_error_counter
                                increment_prompt_error_counter(_get_db_path(), _pid)
                                return f"[ERROR] Prompt '{_pid}' not found on disk"
                            from db.adapters.prompts import increment_prompt_usage_counter
                            increment_prompt_usage_counter(_get_db_path(), _pid)
                            return c
                        except Exception as e:
                            logger.error("[mcp-prompt] Invocation error for %s: %s", _pid, e)
                            raise
                    return _fn
                content_func = _make()
                
            # Build PromptArgument objects from DB data (overrides introspection)
            arguments = []
            for arg_def in (db_args if isinstance(db_args, list) else []):
                if isinstance(arg_def, dict):
                    arguments.append(PromptArgument(
                        name=arg_def.get("name", ""),
                        description=arg_def.get("description"),
                        required=arg_def.get("required", False),
                    ))
            
            # Create FastMCP Prompt object using from_function (proper registration)
            try:
                prompt_obj = Prompt.from_function(
                    content_func,
                    name=prompt_id,
                    title=title,
                    description=description or "",
                )
                # Override arguments with DB-parsed values (fixes **kwargs being shown as "1 arg")
                if db_args and isinstance(db_args, list):
                    prompt_obj.arguments = arguments
                mcp._prompt_manager.add_prompt(prompt_obj)
                logger.debug("[mcp-prompt] Registered: %s args=%d", prompt_id, len(arguments))
            except Exception as e:
                logger.error("[mcp-prompt] Failed to register %s: %s", prompt_id, e)
                
    except ImportError as e:
        logger.warning("[mcp-prompt] DB adapter not available for dynamic registration: %s", e)
    except Exception as e:
        logger.error("[mcp-prompt] Dynamic prompt registration failed: %s", e)


def register_prompts(mcp):
    """Register all MCP prompts on the given FastMCP server instance.

    TYPE=MCP prompts from DB are registered dynamically at each new connection.
    All static (hardcoded) prompts have been removed — everything now comes from
    the engine_prompts table via dynamic registration.

    Args:
        mcp: FastMCP server instance to register prompts on.
    """
    # Register dynamic TYPE=MCP prompts
    _register_dynamic_prompts(mcp)


# ============================================================================
# Skill Resources (SKILLS-MIGRATION, 2026-07-13)
# Registered dynamically from engine_prompts table instead of hardcoded
# @mcp.resource() decorators in qr_mcp_server.py.
# ============================================================================


def _register_skill_resources(mcp):
    """Register skill resources (SKILL.md, SKILL_MCP.md) from DB entries.

    Reads all engine_prompts rows with prompt_id in ('skill', 'skill_mcp')
    and registers each as an MCP resource with URI:
      skill://<prompt_id>

    Example URIs:
      skill://skill     -> returns prompts/skill.md content
      skill://skill_mcp -> returns prompts/skill_mcp.md content

    Args:
        mcp: FastMCP server instance.
    """
    try:
        from db.adapters.prompts import get_skill_entries
        from mcp.server.fastmcp.resources import FunctionResource
        
        db_path = _get_db_path()
        if not db_path:
            logger.warning("[mcp-skill] DB path not found, skipping skill resource registration")
            return
        
        skills = get_skill_entries(db_path)
        if not skills:
            logger.debug("[mcp-skill] No skill entries found in DB")
            return
        
        project_root = _get_project_root()
        
        for skill_data in skills:
            prompt_id = skill_data.get("prompt_id")
            title = skill_data.get("title", prompt_id)
            description = skill_data.get("description", "") or ""
            file_path = skill_data.get("file_path", f"prompts/{prompt_id}.md")
            
            uri = f"skill://{prompt_id}"
            
            # Build absolute path to the skill file
            abs_path = project_root / file_path
            
            if not abs_path.is_file():
                logger.warning("[mcp-skill] Skill file missing: %s", abs_path)
                continue
            
            # Create content function that reads from disk using FunctionResource factory
            def make_skill_reader(filepath, pid=prompt_id):
                """Factory to capture the filepath in a closure."""
                async def read_skill():
                    intact, reason = _verify_file_integrity(pid)
                    if not intact:
                        logger.warning("[mcp-skill] BLOCKED %s: %s", pid, reason)
                        return f"# CHECKSUM MISMATCH\n\n{reason}. Verify and update checksum in edit screen."
                    try:
                        return filepath.read_text(encoding="utf-8")
                    except Exception as e:
                        logger.error("[mcp-skill] Failed to read %s: %s", filepath, e)
                        return f"# Error reading skill: {e}"
                return read_skill
            
            # Register resource using FunctionResource.from_function (proper FastMCP API)
            try:
                resource = FunctionResource.from_function(
                    fn=make_skill_reader(abs_path),
                    uri=uri,
                    name=title,
                    description=description or "",
                    mime_type="text/markdown",
                )
                
                mcp._resource_manager.add_resource(resource)
                logger.debug("[mcp-skill] Registered: %s -> %s", uri, file_path)
            except Exception as e:
                logger.error("[mcp-skill] Failed to register skill %s: %s", prompt_id, e)
        
    except ImportError as e:
        logger.warning("[mcp-skill] DB adapter not available for skill registration: %s", e)
    except Exception as e:
        logger.error("[mcp-skill] Skill resource registration failed: %s", e)


def register_skill_resources(mcp):
    """Public entry point for registering skill resources.
    
    Args:
        mcp: FastMCP server instance.
    """
    _register_skill_resources(mcp)
