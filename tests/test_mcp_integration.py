"""Integration tests — MCPBridge end-to-end with scope enforcement."""
import json
import pytest
from pathlib import Path

from agent_recall.mcp_bridge import MCPBridge
from agent_recall.config import MemoryConfig


@pytest.fixture
def config(tmp_path):
    return MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
        agent_types={"orchestrator": ["coordinator"]},
    )


@pytest.fixture
def bridge(config):
    b = MCPBridge(config.db_path, default_scope="proj-a",
                  scope_chain=["global", "acme", "proj-a"], config=config)
    yield b
    b.close()


@pytest.fixture
def orchestrator_bridge(config):
    b = MCPBridge(config.db_path, default_scope="global",
                  scope_chain=["global"], config=config, scope_reads=False)
    yield b
    b.close()


class TestCreateAndRead:
    def test_create_entity_and_search(self, bridge):
        result = bridge.create_entities([
            {"name": "Alice", "entityType": "person",
             "observations": ["Software engineer at Acme"]},
        ])
        assert result["created"] == 1
        assert result["blocked"] == []

        found = bridge.search_nodes("Alice")
        assert len(found) == 1
        assert found[0]["name"] == "Alice"
        assert "Software engineer at Acme" in found[0]["observations"]

    def test_create_entity_and_open_nodes(self, bridge):
        bridge.create_entities([
            {"name": "Bob", "entityType": "person",
             "observations": ["Designer"]},
        ])
        nodes = bridge.open_nodes(["Bob"])
        assert len(nodes) == 1
        assert nodes[0]["name"] == "Bob"
        assert nodes[0]["entityType"] == "person"

    def test_add_observations(self, bridge):
        bridge.create_entities([
            {"name": "Carol", "entityType": "person", "observations": []},
        ])
        result = bridge.add_observations([
            {"entityName": "Carol", "contents": ["Likes Python", "Uses vim"]},
        ])
        assert result["added"] == 2
        nodes = bridge.open_nodes(["Carol"])
        assert len(nodes[0]["observations"]) == 2

    def test_create_and_delete_entity(self, bridge):
        bridge.create_entities([
            {"name": "Temp", "entityType": "person", "observations": ["test"]},
        ])
        result = bridge.delete_entities(["Temp"])
        assert result["deleted"] == 1
        assert bridge.search_nodes("Temp") == []


class TestRelations:
    def test_create_and_read_relation(self, bridge):
        bridge.create_entities([
            {"name": "Alice", "entityType": "person", "observations": []},
            {"name": "Acme", "entityType": "project", "observations": []},
        ])
        result = bridge.create_relations([
            {"from": "Alice", "to": "Acme", "relationType": "works_on"},
        ])
        assert result["created"] == 1

        graph = bridge.read_graph()
        rels = graph["relations"]
        assert any(r["from"] == "Alice" and r["to"] == "Acme" for r in rels)

    def test_delete_relation(self, bridge):
        bridge.create_entities([
            {"name": "X", "entityType": "entity", "observations": []},
            {"name": "Y", "entityType": "entity", "observations": []},
        ])
        bridge.create_relations([
            {"from": "X", "to": "Y", "relationType": "linked"},
        ])
        result = bridge.delete_relations([
            {"from": "X", "to": "Y", "relationType": "linked"},
        ])
        assert result["deleted"] == 1


class TestScopeEnforcement:
    def test_cross_scope_write_blocked(self, config):
        """Agent A creates entity in its scope. Agent B cannot write to it."""
        bridge_a = MCPBridge(config.db_path, default_scope="proj-a",
                             scope_chain=["global", "acme", "proj-a"], config=config)
        bridge_a.create_entities([
            {"name": "Secret", "entityType": "entity",
             "observations": ["Belongs to proj-a"]},
        ])
        bridge_a.close()

        # Agent from different scope
        bridge_b = MCPBridge(config.db_path, default_scope="other",
                             scope_chain=["global", "other"], config=config)
        result = bridge_b.add_observations([
            {"entityName": "Secret", "contents": ["Injected!"]},
        ])
        assert result["blocked"]
        assert result["added"] == 0
        bridge_b.close()

    def test_cross_scope_relation_blocked(self, config):
        """Cannot create relation FROM entity outside scope."""
        bridge_a = MCPBridge(config.db_path, default_scope="proj-a",
                             scope_chain=["global", "acme", "proj-a"], config=config)
        bridge_a.create_entities([
            {"name": "Owned", "entityType": "entity",
             "observations": ["Has proj-a scope data"]},
        ])
        bridge_a.close()

        bridge_b = MCPBridge(config.db_path, default_scope="other",
                             scope_chain=["global", "other"], config=config)
        bridge_b.create_entities([
            {"name": "Foreign", "entityType": "entity", "observations": []},
        ])
        # Try to create relation FROM foreign-scope entity
        result = bridge_b.create_relations([
            {"from": "Owned", "to": "Foreign", "relationType": "linked"},
        ])
        assert result["blocked"]
        assert result["created"] == 0
        bridge_b.close()

    def test_orchestrator_reads_all(self, bridge, orchestrator_bridge):
        """Orchestrator with scope_reads=False sees all entities."""
        bridge.create_entities([
            {"name": "ScopedEntity", "entityType": "entity",
             "observations": ["In proj-a scope"]},
        ])
        # Orchestrator should see it
        found = orchestrator_bridge.search_nodes("ScopedEntity")
        assert len(found) == 1


class TestInputLimits:
    def test_entity_name_too_long(self, bridge):
        result = bridge.create_entities([
            {"name": "A" * 600, "entityType": "person", "observations": []},
        ])
        assert result["blocked"]
        assert result["created"] == 0

    def test_observation_too_long(self, bridge):
        bridge.create_entities([
            {"name": "Eve", "entityType": "person", "observations": []},
        ])
        result = bridge.add_observations([
            {"entityName": "Eve", "contents": ["X" * 20000]},
        ])
        assert result["blocked"]
        assert result["added"] == 0

    def test_search_limit_cap(self, bridge):
        """Limit above MAX_LIMIT is capped."""
        # Should not crash even with absurd limit
        bridge.search_nodes("test", limit=999999)

    def test_search_long_query_truncated(self, bridge):
        """Very long query is truncated, not crashed."""
        bridge.search_nodes("word " * 1000)


class TestReadGraphDedup:
    def test_no_duplicate_relations(self, bridge):
        bridge.create_entities([
            {"name": "P1", "entityType": "entity", "observations": []},
            {"name": "P2", "entityType": "entity", "observations": []},
        ])
        bridge.create_relations([
            {"from": "P1", "to": "P2", "relationType": "linked"},
        ])
        graph = bridge.read_graph()
        linked = [r for r in graph["relations"]
                  if r["from"] == "P1" and r["to"] == "P2"]
        assert len(linked) == 1
