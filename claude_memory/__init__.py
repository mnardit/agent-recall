"""claude-memory — Persistent memory with scope hierarchy for AI agents."""

__version__ = "0.1.0"

from claude_memory.store import MemoryStore
from claude_memory.hierarchy import ScopedView
from claude_memory.config import MemoryConfig, load_config

__all__ = ["MemoryStore", "ScopedView", "MemoryConfig", "load_config", "__version__"]
