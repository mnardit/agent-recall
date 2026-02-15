#!/usr/bin/env python3
"""MCP server for agent memory — drop-in for Claude Code.

Exposes tools: create_entities, create_relations, add_observations,
delete_entities, delete_relations, delete_observations, read_graph, search_nodes, open_nodes.

Usage in .mcp.json:
    "command": "python3",
    "args": ["-m", "claude_memory.mcp_server"]
"""
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from claude_memory.mcp_bridge import MCPBridge
from claude_memory.config import MemoryConfig, load_config

bridge: MCPBridge | None = None


def _bridge() -> MCPBridge:
    global bridge
    if bridge is None:
        config = load_config()
        slug = Path.cwd().name
        agent = config.get_agent(slug)
        scope = agent.chain[-1] if agent.chain else "global"
        bridge = MCPBridge(config.db_path, default_scope=scope,
                           scope_chain=agent.chain, config=config)
    return bridge


mcp = FastMCP("memory")


@mcp.tool()
def create_entities(entities: list[dict]) -> str:
    """Create multiple new entities in the knowledge graph."""
    return json.dumps(_bridge().create_entities(entities))


@mcp.tool()
def create_relations(relations: list[dict]) -> str:
    """Create multiple new relations between entities."""
    return json.dumps(_bridge().create_relations(relations))


@mcp.tool()
def add_observations(observations: list[dict]) -> str:
    """Add new observations to existing entities."""
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
    """Read the entire knowledge graph."""
    return json.dumps(_bridge().read_graph(), ensure_ascii=False)


@mcp.tool()
def search_nodes(query: str) -> str:
    """Search for nodes by name or observation content."""
    return json.dumps(_bridge().search_nodes(query), ensure_ascii=False)


@mcp.tool()
def open_nodes(names: list[str]) -> str:
    """Open specific nodes by their names."""
    return json.dumps(_bridge().open_nodes(names), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
