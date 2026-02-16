"""claude-memory — Persistent memory with scope hierarchy for AI agents."""

__version__ = "0.1.0"

from claude_memory.store import MemoryStore
from claude_memory.hierarchy import ScopedView
from claude_memory.config import MemoryConfig, AgentConfig, load_config
from claude_memory.mcp_bridge import MCPBridge
from claude_memory.context import assemble_context
from claude_memory.context_gen import (
    get_agent_status,
    get_all_statuses,
    get_generation_logs,
    generate_briefing,
    generate_all,
    LLMResult,
)

__all__ = [
    "MemoryStore", "ScopedView", "MCPBridge",
    "MemoryConfig", "AgentConfig", "load_config",
    "assemble_context",
    "get_agent_status", "get_all_statuses",
    "get_generation_logs",
    "generate_briefing", "generate_all",
    "LLMResult",
    "__version__",
]
