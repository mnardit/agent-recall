#!/usr/bin/env python3
"""MCP server for agent memory — drop-in for any MCP client.

Exposes tools: create_entities, create_relations, add_observations,
delete_entities, delete_relations, delete_observations, read_graph, search_nodes, open_nodes.

Usage in MCP config:
    "command": "python3",
    "args": ["-m", "agent_recall.mcp_server"]
"""
import json
import os
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    raise ImportError(
        "MCP server requires the 'mcp' package. "
        "Install with: pip install 'agent-recall[mcp]'"
    ) from e

from agent_recall.mcp_bridge import MCPBridge
from agent_recall.config import load_config

# Singleton bridge — safe for MCP stdio (single-threaded). Not thread-safe.
bridge: MCPBridge | None = None


def _bridge() -> MCPBridge:
    global bridge
    if bridge is None:
        config = load_config()
        slug = os.environ.get("AGENT_RECALL_SLUG") or Path.cwd().name
        agent = config.get_agent(slug)
        scope = agent.chain[-1] if agent.chain else "global"
        scope_reads = agent.agent_type != "orchestrator"
        bridge = MCPBridge(config.db_path, default_scope=scope,
                           scope_chain=agent.chain, config=config,
                           scope_reads=scope_reads)
    return bridge


mcp = FastMCP(
    "memory",
    instructions=(
        "You have a persistent knowledge graph that survives across sessions. "
        "PROACTIVELY save important information as you encounter it during conversation — "
        "don't wait to be asked.\n\n"
        "Save:\n"
        "- People: names, roles, contact info, preferences, communication style\n"
        "- Decisions: technical choices, agreements, rationale\n"
        "- Facts: project status, deadlines, blockers, dependencies\n"
        "- Context: meeting outcomes, requirements, priorities\n\n"
        "Before creating an entity, use search_nodes to check if it already exists. "
        "Add observations to existing entities rather than creating duplicates.\n"
        "Don't save trivial or ephemeral information (typos, one-off debug values, etc.)."
    ),
)


@mcp.tool()
def create_entities(entities: list[dict]) -> str:
    """Create new entities (people, projects, tools, concepts) in the knowledge graph.

    Use when you encounter someone or something worth remembering across sessions.
    Always search_nodes first to avoid duplicates.

    Each entity: {"name": "Alice", "entityType": "person", "observations": ["Lead engineer at Acme"]}
    """
    return json.dumps(_bridge().create_entities(entities))


@mcp.tool()
def create_relations(relations: list[dict]) -> str:
    """Link entities together in the knowledge graph.

    Use when you discover relationships: works_at, manages, depends_on, contact_for, etc.

    Each relation: {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    """
    return json.dumps(_bridge().create_relations(relations))


@mcp.tool()
def add_observations(observations: list[dict]) -> str:
    """Add new facts to existing entities. This is the most common write operation.

    Use when you learn something new about a known person, project, or concept —
    roles, decisions, preferences, status changes, meeting outcomes.

    Each observation: {"entityName": "Alice", "contents": ["Prefers async communication"]}
    """
    return json.dumps(_bridge().add_observations(observations))


@mcp.tool()
def delete_entities(entityNames: list[str]) -> str:
    """Delete entities and their relations from the knowledge graph."""
    return json.dumps(_bridge().delete_entities(entityNames))


@mcp.tool()
def delete_relations(relations: list[dict]) -> str:
    """Delete relations from the knowledge graph."""
    return json.dumps(_bridge().delete_relations(relations))


@mcp.tool()
def delete_observations(deletions: list[dict]) -> str:
    """Delete specific observations from entities."""
    return json.dumps(_bridge().delete_observations(deletions))


@mcp.tool()
def read_graph() -> str:
    """Read the full knowledge graph. Use sparingly — prefer search_nodes for targeted lookups."""
    return json.dumps(_bridge().read_graph(), ensure_ascii=False)


@mcp.tool()
def search_nodes(query: str, limit: int = 10) -> str:
    """Search the knowledge graph by name or content.

    Use BEFORE creating entities to check for duplicates.
    Also use to recall information about people, projects, or decisions.

    Returns up to `limit` results (default 10), filtered by scope.
    """
    return json.dumps(_bridge().search_nodes(query, limit=limit), ensure_ascii=False)


@mcp.tool()
def open_nodes(names: list[str]) -> str:
    """Retrieve full details for specific entities by name.

    Use when you need complete context about known people, projects, or concepts.
    """
    return json.dumps(_bridge().open_nodes(names), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
