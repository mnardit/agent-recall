"""YAML-based configuration for claude-memory."""
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATHS = [
    Path.cwd() / "memory.yaml",
    Path.home() / ".claude" / "memory" / "memory.yaml",
]
DEFAULT_DB_PATH = Path.home() / ".claude" / "memory" / "frames.db"
DEFAULT_CACHE_DIR = Path.home() / ".claude" / "memory" / "context_cache"


@dataclass
class AgentConfig:
    slug: str
    tier: int = 2
    chain: list[str] = field(default_factory=lambda: ["global"])
    agent_type: str | None = None


@dataclass
class MemoryConfig:
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    cache_dir: Path = field(default_factory=lambda: DEFAULT_CACHE_DIR)
    hierarchy: dict[str, list[str]] = field(default_factory=dict)
    tiers: dict[int, list[str]] = field(default_factory=dict)
    agent_types: dict[str, list[str]] = field(default_factory=dict)
    briefing: dict[str, Any] = field(default_factory=dict)
    extra_context: dict[str, str] = field(default_factory=dict)
    templates_dir: Path | None = None
    vault_dir: Path | None = None
    vault_task_header: str = "## Tasks"
    vault_auto_commit: bool = True

    def get_agent(self, slug: str) -> AgentConfig:
        """Infer agent config from slug using hierarchy + tiers + agent_types."""
        # Check orchestrator
        if slug in self.agent_types.get("orchestrator", []):
            return AgentConfig(slug=slug, tier=3, chain=["global"],
                               agent_type="orchestrator")
        # Check explicit tiers
        for tier, slugs in self.tiers.items():
            if slug in slugs:
                chain = [] if tier == 0 else ["global", slug]
                return AgentConfig(slug=slug, tier=tier, chain=chain)
        # Check system agents
        if slug in self.agent_types.get("system", []):
            return AgentConfig(slug=slug, tier=1, chain=["global", slug],
                               agent_type="system")
        # Check hierarchy children
        for parent, children in self.hierarchy.items():
            if slug in children:
                return AgentConfig(slug=slug, tier=2,
                                   chain=["global", parent, slug])
        # Check if slug IS a parent
        if slug in self.hierarchy:
            return AgentConfig(slug=slug, tier=2, chain=["global", slug])
        # Default: tier 2 standalone
        return AgentConfig(slug=slug, tier=2, chain=["global", slug])

    def get_agent_type(self, slug: str) -> str:
        """Determine agent type for prompt template selection."""
        agent = self.get_agent(slug)
        if agent.agent_type:
            return agent.agent_type
        for type_name, slugs in self.agent_types.items():
            if slug in slugs:
                return type_name
        # Infer from hierarchy
        for parent, children in self.hierarchy.items():
            if slug in children:
                return "client"
        if slug in self.hierarchy:
            return "agency"
        # Check tier
        if agent.tier == 0:
            return "system"
        return "personal"

    def scope_children(self, scope: str) -> set[str]:
        """Get children of a scope from hierarchy."""
        return set(self.hierarchy.get(scope, []))

    def all_agents(self) -> list[str]:
        """Return list of all known agent slugs."""
        agents: set[str] = set()
        for slugs in self.tiers.values():
            agents.update(slugs)
        for type_slugs in self.agent_types.values():
            agents.update(type_slugs)
        for parent, children in self.hierarchy.items():
            agents.add(parent)
            agents.update(children)
        return sorted(agents)


def _expand_path(raw: str) -> Path:
    """Expand ~ and env vars in path strings."""
    import os
    return Path(os.path.expandvars(raw)).expanduser()


def load_config(path: Path | str | None = None) -> MemoryConfig:
    """Load config from YAML file. Searches default paths if none given."""
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        return _parse_config(config_path)

    # Compute fresh each time so tests can monkeypatch cwd/HOME
    candidates = [
        Path.cwd() / "memory.yaml",
        Path.home() / ".claude" / "memory" / "memory.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return _parse_config(candidate)

    return MemoryConfig()


def _parse_config(config_path: Path) -> MemoryConfig:
    """Parse a YAML config file into MemoryConfig."""
    data = yaml.safe_load(config_path.read_text()) or {}

    hierarchy: dict[str, list[str]] = {}
    for parent, children in data.get("hierarchy", {}).items():
        if isinstance(children, dict):
            hierarchy[parent] = children.get("children", [])
        elif isinstance(children, list):
            hierarchy[parent] = children
        else:
            hierarchy[parent] = []

    tiers: dict[int, list[str]] = {}
    for tier_key, slugs in data.get("tiers", {}).items():
        tiers[int(tier_key)] = slugs

    agent_types: dict[str, list[str]] = {}
    for type_name, slugs in data.get("agent_types", {}).items():
        agent_types[type_name] = slugs

    briefing = data.get("briefing", {})

    extra_context: dict[str, str] = {}
    for slug, text in data.get("extra_context", {}).items():
        if isinstance(text, str):
            extra_context[slug] = text

    templates_dir = None
    if "templates_dir" in data:
        templates_dir = _expand_path(data["templates_dir"])
    elif "briefing" in data and "templates_dir" in data["briefing"]:
        templates_dir = _expand_path(data["briefing"]["templates_dir"])

    vault_dir = None
    vault_task_header = "## Tasks"
    vault_auto_commit = True
    vault_cfg = data.get("vault", {})
    if isinstance(vault_cfg, dict):
        if "dir" in vault_cfg:
            vault_dir = _expand_path(vault_cfg["dir"])
        vault_task_header = vault_cfg.get("task_header", "## Tasks")
        vault_auto_commit = vault_cfg.get("auto_commit", True)

    return MemoryConfig(
        db_path=_expand_path(data["db_path"]) if "db_path" in data else DEFAULT_DB_PATH,
        cache_dir=_expand_path(data["cache_dir"]) if "cache_dir" in data else DEFAULT_CACHE_DIR,
        hierarchy=hierarchy,
        tiers=tiers,
        agent_types=agent_types,
        briefing=briefing,
        extra_context=extra_context,
        templates_dir=templates_dir,
        vault_dir=vault_dir,
        vault_task_header=vault_task_header,
        vault_auto_commit=vault_auto_commit,
    )
