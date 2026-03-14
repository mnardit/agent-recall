"""agent-recall — Persistent memory with scope hierarchy for AI agents."""

__version__ = "0.4.0"

from agent_recall.store import MemoryStore
from agent_recall.hierarchy import ScopedView
from agent_recall.config import MemoryConfig, AgentConfig, load_config
from agent_recall.mcp_bridge import MCPBridge
from agent_recall.context import assemble_context
from agent_recall.context_gen import (
    get_agent_status,
    get_all_statuses,
    get_generation_logs,
    generate_briefing,
    generate_all,
    LLMResult,
)
from agent_recall.migrations import get_version, run_migrations, LATEST_VERSION

__all__ = [
    "MemoryStore", "ScopedView", "MCPBridge",
    "MemoryConfig", "AgentConfig", "load_config",
    "assemble_context",
    "get_agent_status", "get_all_statuses",
    "get_generation_logs",
    "generate_briefing", "generate_all",
    "LLMResult",
    "get_version", "run_migrations", "LATEST_VERSION",
    "__version__",
]
