"""Tests for MCP bridge — scope enforcement and CRUD operations."""
import pytest
from agent_recall.config import MemoryConfig
from agent_recall.mcp_bridge import MCPBridge


@pytest.fixture
def bridge(tmp_path):
    """Unrestricted bridge (chain len 1 = orchestrator, no enforcement)."""
    b = MCPBridge(tmp_path / "test.db", default_scope="global",
                  scope_chain=["global"])
    yield b
    b.close()


@pytest.fixture
def scoped_bridge(tmp_path):
    """Scoped bridge with enforcement (chain len 2)."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    b = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                  scope_chain=["global", "acme", "proj-a"], config=config)
    yield b
    b.close()


# --- CRUD operations ---

def test_create_entities(bridge):
    result = bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["works at Acme"]},
        {"name": "Bob", "entityType": "person"},
    ])
    assert result["created"] == 2
    assert result["blocked"] == []


def test_create_entities_with_observations(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["engineer", "likes coffee"]}
    ])
    nodes = bridge.open_nodes(["Alice"])
    assert len(nodes) == 1
    assert set(nodes[0]["observations"]) == {"engineer", "likes coffee"}


def test_create_relations(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person"},
        {"name": "Acme", "entityType": "client"},
    ])
    result = bridge.create_relations([
        {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    ])
    assert result["created"] == 1


def test_create_relation_auto_creates_entity(bridge):
    """If entity doesn't exist yet, relation creation auto-creates it."""
    result = bridge.create_relations([
        {"from": "NewPerson", "to": "NewOrg", "relationType": "member_of"}
    ])
    assert result["created"] == 1
    nodes = bridge.open_nodes(["NewPerson"])
    assert len(nodes) == 1


def test_add_observations(bridge):
    bridge.create_entities([{"name": "Alice", "entityType": "person"}])
    result = bridge.add_observations([
        {"entityName": "Alice", "contents": ["fact one", "fact two"]}
    ])
    assert result["added"] == 2


def test_add_observations_nonexistent(bridge):
    result = bridge.add_observations([
        {"entityName": "Ghost", "contents": ["invisible"]}
    ])
    assert result["added"] == 0
    assert any("Ghost" in b for b in result["blocked"])


def test_delete_entities(bridge):
    bridge.create_entities([{"name": "Alice", "entityType": "person"}])
    result = bridge.delete_entities(["Alice"])
    assert result["deleted"] == 1
    assert bridge.open_nodes(["Alice"]) == []


def test_delete_entities_nonexistent(bridge):
    result = bridge.delete_entities(["Ghost"])
    assert result["deleted"] == 0
    assert any("Ghost" in b for b in result["blocked"])


def test_delete_relations(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person"},
        {"name": "Acme", "entityType": "client"},
    ])
    bridge.create_relations([
        {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    ])
    result = bridge.delete_relations([
        {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    ])
    assert result["deleted"] == 1


def test_delete_observations(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["fact to delete", "fact to keep"]}
    ])
    result = bridge.delete_observations([
        {"entityName": "Alice", "observations": ["fact to delete"]}
    ])
    assert result["deleted"] == 1
    nodes = bridge.open_nodes(["Alice"])
    assert nodes[0]["observations"] == ["fact to keep"]


def test_delete_observations_nonexistent(bridge):
    result = bridge.delete_observations([
        {"entityName": "Ghost", "observations": ["something"]}
    ])
    assert result["deleted"] == 0
    assert any("Ghost" in b for b in result["blocked"])


def test_delete_relations_nonexistent(bridge):
    result = bridge.delete_relations([
        {"from": "Ghost", "to": "Phantom", "relationType": "knows"}
    ])
    assert result["deleted"] == 0
    assert any("Ghost" in b for b in result["blocked"])


# --- Read operations ---

def test_open_nodes(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["engineer"]}
    ])
    nodes = bridge.open_nodes(["Alice", "Nonexistent"])
    assert len(nodes) == 1
    assert nodes[0]["name"] == "Alice"
    assert nodes[0]["entityType"] == "person"


def test_search_nodes(bridge):
    bridge.create_entities([
        {"name": "Alice Smith", "entityType": "person"},
        {"name": "Bob Jones", "entityType": "person"},
    ])
    results = bridge.search_nodes("Alice")
    assert len(results) == 1
    assert results[0]["name"] == "Alice Smith"


def test_read_graph(bridge):
    bridge.create_entities([
        {"name": "Alice", "entityType": "person"},
        {"name": "Acme", "entityType": "client"},
    ])
    bridge.create_relations([
        {"from": "Alice", "to": "Acme", "relationType": "works_at"}
    ])
    graph = bridge.read_graph()
    assert len(graph["entities"]) == 2
    assert len(graph["relations"]) == 1
    assert graph["relations"][0]["relationType"] == "works_at"


# --- Scope enforcement ---

def test_orchestrator_no_enforcement(bridge):
    """Chain length 1 (orchestrator) — no scope enforcement."""
    bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["data"]}
    ])
    # Can always write
    result = bridge.add_observations([
        {"entityName": "Alice", "contents": ["more data"]}
    ])
    assert result["added"] == 1
    assert result["blocked"] == []


def test_scoped_write_allowed(scoped_bridge):
    """Agent can write to entity in its own scope."""
    scoped_bridge.create_entities([
        {"name": "Alice", "entityType": "person",
         "observations": ["works on proj-a"]}
    ])
    # Entity has obs in proj-a scope (agent's scope), so it can write
    result = scoped_bridge.add_observations([
        {"entityName": "Alice", "contents": ["another fact"]}
    ])
    assert result["added"] == 1


def test_scoped_write_blocked(tmp_path):
    """Agent can't write to entity belonging to different scope."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    # First create entity in proj-b scope using a proj-b bridge
    b_other = MCPBridge(tmp_path / "test.db", default_scope="proj-b",
                        scope_chain=["global", "acme", "proj-b"], config=config)
    b_other.create_entities([
        {"name": "SecretDoc", "entityType": "entity",
         "observations": ["classified"]}
    ])
    b_other.close()

    # Now try to write from proj-a bridge
    b_a = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                    scope_chain=["global", "acme", "proj-a"], config=config)
    result = b_a.add_observations([
        {"entityName": "SecretDoc", "contents": ["hacked"]}
    ])
    assert result["added"] == 0
    assert len(result["blocked"]) == 1
    assert "Write blocked" in result["blocked"][0]
    b_a.close()


def test_scoped_delete_blocked(tmp_path):
    """Agent can't delete entity belonging to different scope."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    b_other = MCPBridge(tmp_path / "test.db", default_scope="proj-b",
                        scope_chain=["global", "acme", "proj-b"], config=config)
    b_other.create_entities([
        {"name": "ProtectedEntity", "entityType": "entity",
         "observations": ["belongs to proj-b"]}
    ])
    b_other.close()

    b_a = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                    scope_chain=["global", "acme", "proj-a"], config=config)
    result = b_a.delete_entities(["ProtectedEntity"])
    assert result["deleted"] == 0
    assert len(result["blocked"]) == 1
    b_a.close()


def test_global_only_entity_writable_by_anyone(tmp_path):
    """Entity with only global-scope data is writable by any scoped agent."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a"]},
    )
    # Create entity from orchestrator (global scope)
    b_orch = MCPBridge(tmp_path / "test.db", default_scope="global",
                       scope_chain=["global"])
    b_orch.create_entities([{"name": "SharedDoc", "entityType": "entity"}])
    b_orch.close()

    # Scoped agent can write to it (no specific scopes = global only)
    b_a = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                    scope_chain=["global", "acme", "proj-a"], config=config)
    result = b_a.add_observations([
        {"entityName": "SharedDoc", "contents": ["added by proj-a"]}
    ])
    assert result["added"] == 1
    b_a.close()


def test_parent_scope_allows_child_access(tmp_path):
    """Agent at parent scope can access children via config hierarchy."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    # Create entity in proj-a
    b_a = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                    scope_chain=["global", "acme", "proj-a"], config=config)
    b_a.create_entities([
        {"name": "ChildEntity", "entityType": "entity",
         "observations": ["in proj-a"]}
    ])
    b_a.close()

    # Parent (acme) agent can write to it
    b_parent = MCPBridge(tmp_path / "test.db", default_scope="acme",
                         scope_chain=["global", "acme"], config=config)
    result = b_parent.add_observations([
        {"entityName": "ChildEntity", "contents": ["from parent"]}
    ])
    assert result["added"] == 1
    b_parent.close()


# --- strict_scopes ---

def test_strict_scopes_rejects_unknown(tmp_path):
    """strict_scopes=True raises ValueError for scope not in config."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    with pytest.raises(ValueError, match="not in known scopes"):
        MCPBridge(tmp_path / "test.db", default_scope="typo-scope",
                  scope_chain=["global", "acme", "typo-scope"],
                  config=config, strict_scopes=True)


def test_strict_scopes_accepts_known(tmp_path):
    """strict_scopes=True succeeds for scope in config hierarchy."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    b = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                  scope_chain=["global", "acme", "proj-a"],
                  config=config, strict_scopes=True)
    b.close()


def test_strict_scopes_accepts_parent(tmp_path):
    """strict_scopes=True succeeds for parent scope in hierarchy."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a"]},
    )
    b = MCPBridge(tmp_path / "test.db", default_scope="acme",
                  scope_chain=["global", "acme"],
                  config=config, strict_scopes=True)
    b.close()


def test_strict_scopes_off_by_default(tmp_path):
    """Default strict_scopes=False allows unknown scope without error."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a"]},
    )
    b = MCPBridge(tmp_path / "test.db", default_scope="unknown-scope",
                  scope_chain=["global", "unknown-scope"],
                  config=config)
    b.close()


def test_strict_scopes_no_config_ignored(tmp_path):
    """strict_scopes=True without config does not raise."""
    b = MCPBridge(tmp_path / "test.db", default_scope="anything",
                  scope_chain=["global", "anything"],
                  strict_scopes=True)
    b.close()


def test_strict_scopes_global_always_valid(tmp_path):
    """Global scope is always valid even with strict_scopes."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a"]},
    )
    b = MCPBridge(tmp_path / "test.db", default_scope="global",
                  scope_chain=["global"],
                  config=config, strict_scopes=True)
    b.close()


def test_scoped_delete_relations_blocked(tmp_path):
    """Agent can't delete relation between entities outside its scope."""
    config = MemoryConfig(
        db_path=tmp_path / "test.db",
        hierarchy={"acme": ["proj-a", "proj-b"]},
    )
    # proj-b creates two entities and a relation
    b_b = MCPBridge(tmp_path / "test.db", default_scope="proj-b",
                    scope_chain=["global", "acme", "proj-b"], config=config)
    b_b.create_entities([
        {"name": "Alice", "entityType": "person", "observations": ["works at B"]},
        {"name": "ProjB", "entityType": "project", "observations": ["B project"]},
    ])
    b_b.create_relations([
        {"from": "Alice", "to": "ProjB", "relationType": "works_on"},
    ])
    b_b.close()

    # proj-a tries to delete that relation — blocked
    b_a = MCPBridge(tmp_path / "test.db", default_scope="proj-a",
                    scope_chain=["global", "acme", "proj-a"], config=config)
    result = b_a.delete_relations([
        {"from": "Alice", "to": "ProjB", "relationType": "works_on"},
    ])
    assert result["deleted"] == 0
    assert len(result["blocked"]) == 1
    assert "Blocked" in result["blocked"][0]
    b_a.close()
