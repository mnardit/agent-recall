import sqlite3

import pytest
from agent_recall.store import MemoryStore


# --- Entities ---

def test_create_store(store):
    assert store.db_path.exists()

def test_create_entity(store):
    eid = store.create_entity("Alice", "person")
    assert eid >= 1

def test_find_entity(store):
    eid = store.create_entity("Alice", "person")
    assert store.find_entity("Alice") == eid
    assert store.find_entity("Alice", "person") == eid
    assert store.find_entity("Alice", "client") is None
    assert store.find_entity("Nobody") is None

def test_resolve_entity_creates(store):
    eid = store.resolve_entity("Alice", "person")
    assert eid >= 1
    assert store.resolve_entity("Alice", "person") == eid

def test_get_entity(store):
    eid = store.create_entity("Alice", "person")
    entity = store.get_entity(eid)
    assert entity["name"] == "Alice"
    assert entity["type"] == "person"

def test_list_entities(store):
    store.create_entity("Alice", "person")
    store.create_entity("Bob", "person")
    store.create_entity("Acme", "client")
    assert len(store.list_entities(entity_type="person")) == 2
    assert len(store.list_entities(entity_type="client")) == 1

def test_delete_entity(store):
    eid = store.create_entity("Alice", "person")
    store.delete_entity(eid)
    assert store.find_entity("Alice") is None

def test_duplicate_names_prevented(store):
    store.create_entity("Anna", "person")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_entity("Anna", "person")

def test_resolve_entity_concurrent_safe(store):
    id1 = store.resolve_entity("Anna", "person")
    id2 = store.resolve_entity("Anna", "person")
    assert id1 == id2

def test_empty_name_rejected(store):
    with pytest.raises(ValueError, match="name cannot be empty"):
        store.create_entity("", "person")
    with pytest.raises(ValueError, match="name cannot be empty"):
        store.create_entity("  ", "person")
    with pytest.raises(ValueError, match="name cannot be empty"):
        store.resolve_entity("", "person")

# --- Slots (scope-aware) ---

def test_set_and_get_slot(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "company", "Acme")
    assert store.get_slot(eid, "company") == "Acme"

def test_get_slot_nonexistent(store):
    eid = store.resolve_entity("Alice", "person")
    assert store.get_slot(eid, "company") is None

def test_set_slot_overwrites_same_scope(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "company", "Acme", scope="global")
    store.set_slot(eid, "company", "BigCo", scope="global")
    assert store.get_slot(eid, "company") == "BigCo"

def test_set_slot_different_scopes(store):
    eid = store.resolve_entity("Acme", "agency")
    store.set_slot(eid, "status", "active", scope="acme")
    store.set_slot(eid, "status", "paused", scope="client-a")
    assert store.get_slot(eid, "status", scope_chain=["acme"]) == "active"
    assert store.get_slot(eid, "status", scope_chain=["client-a"]) == "paused"
    assert store.get_slot(eid, "status", scope_chain=["acme", "client-a"]) == "paused"

def test_get_slots_merged(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "name", "Alice", scope="global")
    store.set_slot(eid, "role", "CEO", scope="acme")
    store.set_slot(eid, "email", "alice@acme.com", scope="global")
    slots = store.get_slots(eid, scope_chain=["global", "acme"])
    assert slots["name"] == "Alice"
    assert slots["role"] == "CEO"
    assert slots["email"] == "alice@acme.com"

def test_get_slots_local_overrides_global(store):
    eid = store.resolve_entity("Acme", "agency")
    store.set_slot(eid, "status", "active", scope="global")
    store.set_slot(eid, "status", "paused", scope="client-a")
    slots = store.get_slots(eid, scope_chain=["global", "client-a"])
    assert slots["status"] == "paused"

def test_list_entities_in_scopes(store):
    e1 = store.resolve_entity("Max", "person")
    store.set_slot(e1, "role", "owner", scope="global")
    e2 = store.resolve_entity("Alice", "person")
    store.set_slot(e2, "role", "contact", scope="client-a")
    e3 = store.resolve_entity("Bob", "person")
    store.set_slot(e3, "role", "contact", scope="client-b")
    visible = store.list_entities_in_scopes(["global", "client-a"], entity_type="person")
    names = {e["name"] for e in visible}
    assert names == {"Max", "Alice"}

# --- History ---

def test_slot_history(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "company", "Acme", scope="global")
    store.set_slot(eid, "company", "BigCo", scope="global")
    history = store.get_slot_history(eid, "company")
    assert len(history) == 2
    assert history[0]["value"] == "Acme"
    assert history[0]["valid_to"] is not None
    assert history[1]["value"] == "BigCo"
    assert history[1]["valid_to"] is None

def test_archive_slot(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "company", "Acme")
    store.archive_slot(eid, "company")
    assert store.get_slot(eid, "company") is None
    assert len(store.get_slot_history(eid, "company")) == 1

# --- Observations ---

def test_add_and_get_observations(store):
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "Works at Acme")
    store.add_observation(eid, "Prefers async comms")
    obs = store.get_observations(eid)
    assert len(obs) == 2
    assert obs[0]["text"] == "Works at Acme"

def test_archive_observation(store):
    eid = store.resolve_entity("Alice", "person")
    oid = store.add_observation(eid, "Old fact")
    store.archive_observation(oid)
    assert len(store.get_observations(eid)) == 0
    assert len(store.get_observations(eid, include_archived=True)) == 1

def test_delete_observation_by_text(store):
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "Fact A")
    store.add_observation(eid, "Fact B")
    store.delete_observation_by_text(eid, "Fact A")
    obs = store.get_observations(eid)
    assert len(obs) == 1
    assert obs[0]["text"] == "Fact B"

# --- Relations ---

def test_add_and_get_relation(store):
    e1 = store.resolve_entity("Alice", "person")
    e2 = store.resolve_entity("Acme", "client")
    store.add_relation(e1, e2, "works_at")
    rels = store.get_relations(e1)
    assert len(rels) == 1
    assert rels[0]["to_name"] == "Acme"
    assert rels[0]["status"] == "active"

def test_archive_relation(store):
    e1 = store.resolve_entity("Alice", "person")
    e2 = store.resolve_entity("Acme", "client")
    rid = store.add_relation(e1, e2, "works_at")
    store.archive_relation(rid)
    assert len(store.get_relations(e1)) == 0
    assert len(store.get_relations(e1, include_archived=True)) == 1

def test_reverse_relations(store):
    e1 = store.resolve_entity("Alice", "person")
    e2 = store.resolve_entity("Bob", "person")
    e3 = store.resolve_entity("Acme", "client")
    store.add_relation(e1, e3, "works_at")
    store.add_relation(e2, e3, "works_at")
    who = store.get_reverse_relations(e3, "works_at")
    assert {r["from_name"] for r in who} == {"Alice", "Bob"}

# --- Log Entries ---

def test_add_and_get_logs(store):
    eid = store.resolve_entity("Project X", "project")
    store.add_log(eid, "Kickoff meeting with stakeholders")
    logs = store.get_logs(eid)
    assert len(logs) == 1
    assert "stakeholders" in logs[0]["text"]

def test_logs_ordered(store):
    eid = store.resolve_entity("Proj", "project")
    store.add_log(eid, "First")
    store.add_log(eid, "Second")
    logs = store.get_logs(eid)
    assert logs[0]["text"] == "First"
    assert logs[1]["text"] == "Second"

def test_log_with_date(store):
    eid = store.resolve_entity("Proj", "project")
    store.add_log(eid, "Event", date="2026-02-10")
    logs = store.get_logs(eid)
    assert logs[0]["date"] == "2026-02-10"

# --- Documents ---

def test_save_and_get_document(store):
    store.save_document("my-decision", "decision", "# Title\n\nContent", tags=["architecture"])
    doc = store.get_document("my-decision")
    assert doc["content"] == "# Title\n\nContent"
    assert doc["type"] == "decision"
    assert "architecture" in doc["tags"]

def test_update_document(store):
    store.save_document("my-doc", "decision", "v1")
    store.save_document("my-doc", "decision", "v2")
    assert store.get_document("my-doc")["content"] == "v2"

def test_list_documents(store):
    store.save_document("doc1", "decision", "c1")
    store.save_document("doc2", "process", "c2")
    assert len(store.list_documents(doc_type="decision")) == 1

# --- Search ---

def test_search_by_name(store):
    store.resolve_entity("Alice", "person")
    results = store.search("Alice")
    assert len(results) >= 1

def test_search_by_slot(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "email", "alice@example.com")
    results = store.search("alice@example")
    assert any(r["name"] == "Alice" for r in results)

def test_search_by_observation(store):
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "CEO of Acme Corp")
    results = store.search("CEO")
    assert any(r["name"] == "Alice" for r in results)

def test_search_morphology_stem(store):
    eid = store.resolve_entity("Max", "person")
    store.add_observation(eid, "observation about sandpipers")
    results = store.search("sandpiper")
    assert any(r["name"] == "Max" for r in results)

def test_search_multiword(store):
    e1 = store.resolve_entity("Alice", "person")
    store.add_observation(e1, "works at Google")
    e2 = store.resolve_entity("Bob", "person")
    store.add_observation(e2, "birdwatching expert")
    results = store.search("Google birdwatching")
    names = {r["name"] for r in results}
    assert "Alice" in names
    assert "Bob" in names

# --- New public API methods ---

def test_get_entity_scopes(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="global")
    store.set_slot(eid, "role", "lead", scope="acme")
    store.add_observation(eid, "Key contributor", scope="project-x")
    scopes = store.get_entity_scopes(eid)
    assert "acme" in scopes
    assert "project-x" in scopes
    assert "global" not in scopes

def test_get_entity_scopes_empty(store):
    eid = store.resolve_entity("Bob", "person")
    store.set_slot(eid, "role", "dev", scope="global")
    scopes = store.get_entity_scopes(eid)
    assert scopes == set()

def test_list_entities_with_observations_in_scope(store):
    e1 = store.resolve_entity("Alice", "person")
    store.add_observation(e1, "Works on topic", scope="my-topic")
    e2 = store.resolve_entity("Bob", "person")
    store.add_observation(e2, "Not in this scope", scope="other")
    result = store.list_entities_with_observations_in_scope("my-topic")
    names = {e["name"] for e in result}
    assert "Alice" in names
    assert "Bob" not in names
