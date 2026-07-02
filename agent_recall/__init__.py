"""agent-recall — Persistent memory with scope hierarchy for AI agents."""

__version__ = "0.5.0"

from agent_recall.store import MemoryStore
from agent_recall.hierarchy import ScopedView
from agent_recall.config import (
    MemoryConfig, AgentConfig, load_config,
    EmbeddingConfig, TierConfig, AutoCaptureConfig,
)
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
from agent_recall.decay_engine import DecayEngine, DecayConfig
from agent_recall.token_budget import TokenBudget, BudgetConfig
from agent_recall.privacy import redact, detect_secrets, assign_privacy_level
from agent_recall.vector_search import VectorSearchEngine
from agent_recall.surgical_trim import SurgicalTrimmer
from agent_recall.knowledge_tiers import KnowledgeTierManager
from agent_recall.promotion import KnowledgePromoter
from agent_recall.synthesis import Synthesizer
from agent_recall.auto_capture import AutoCaptureEngine
from agent_recall.entity_linker import EntityLinker
from agent_recall.trust import TrustEngine, TrustReason
from agent_recall.retrieval_feedback import RetrievalFeedback
from agent_recall.prediction import AccessPredictor

__all__ = [
    "MemoryStore", "ScopedView", "MCPBridge",
    "MemoryConfig", "AgentConfig", "load_config",
    "EmbeddingConfig", "TierConfig", "AutoCaptureConfig",
    "assemble_context",
    "get_agent_status", "get_all_statuses",
    "get_generation_logs",
    "generate_briefing", "generate_all",
    "LLMResult",
    "get_version", "run_migrations", "LATEST_VERSION",
    "DecayEngine", "DecayConfig",
    "TokenBudget", "BudgetConfig",
    "redact", "detect_secrets", "assign_privacy_level",
    "VectorSearchEngine", "SurgicalTrimmer",
    "KnowledgeTierManager", "KnowledgePromoter",
    "Synthesizer", "AutoCaptureEngine", "EntityLinker",
    "TrustEngine", "TrustReason",
    "RetrievalFeedback", "AccessPredictor",
    "__version__",
]
