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

# --- find_entities_by_slot ---

def test_find_entities_by_slot_basic(store):
    eid = store.create_entity("Alice", "person")
    store.set_slot(eid, "status", "active", scope="global")
    results = store.find_entities_by_slot("status", "active")
    assert len(results) == 1
    assert results[0]["name"] == "Alice"

def test_find_entities_by_slot_no_match(store):
    eid = store.create_entity("Alice", "person")
    store.set_slot(eid, "status", "active")
    assert store.find_entities_by_slot("status", "inactive") == []
    assert store.find_entities_by_slot("nonexistent") == []

def test_find_entities_by_slot_key_only(store):
    eid1 = store.create_entity("Alice", "person")
    eid2 = store.create_entity("Bob", "person")
    store.set_slot(eid1, "role", "dev")
    store.set_slot(eid2, "role", "manager")
    results = store.find_entities_by_slot("role")
    assert len(results) == 2

def test_find_entities_by_slot_entity_type_filter(store):
    e1 = store.create_entity("Alice", "person")
    e2 = store.create_entity("draft-001", "draft")
    store.set_slot(e1, "status", "pending")
    store.set_slot(e2, "status", "pending")
    results = store.find_entities_by_slot("status", "pending", entity_type="draft")
    assert len(results) == 1
    assert results[0]["name"] == "draft-001"

def test_find_entities_by_slot_scope_filter(store):
    eid = store.create_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="acme")
    store.set_slot(eid, "hobby", "chess", scope="global")
    assert len(store.find_entities_by_slot("role", scope="acme")) == 1
    assert store.find_entities_by_slot("role", scope="global") == []

def test_find_entities_by_slot_ignores_archived(store):
    eid = store.create_entity("Alice", "person")
    store.set_slot(eid, "status", "old")
    store.set_slot(eid, "status", "new")  # archives "old"
    results = store.find_entities_by_slot("status", "old")
    assert results == []
    results = store.find_entities_by_slot("status", "new")
    assert len(results) == 1

def test_find_entities_by_slot_all_filters(store):
    e1 = store.create_entity("topic-a", "topic")
    e2 = store.create_entity("topic-b", "topic")
    e3 = store.create_entity("Alice", "person")
    store.set_slot(e1, "parent_project", "acme", scope="topic-a")
    store.set_slot(e2, "parent_project", "other-org", scope="topic-b")
    store.set_slot(e3, "parent_project", "acme", scope="global")
    results = store.find_entities_by_slot(
        "parent_project", "acme", entity_type="topic", scope="topic-a")
    assert len(results) == 1
    assert results[0]["name"] == "topic-a"

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
    e1 = store.resolve_entity("Jordan", "person")
    store.set_slot(e1, "role", "owner", scope="global")
    e2 = store.resolve_entity("Alice", "person")
    store.set_slot(e2, "role", "contact", scope="client-a")
    e3 = store.resolve_entity("Bob", "person")
    store.set_slot(e3, "role", "contact", scope="client-b")
    visible = store.list_entities_in_scopes(["global", "client-a"], entity_type="person")
    names = {e["name"] for e in visible}
    assert names == {"Jordan", "Alice"}

def test_list_entities_in_scopes_observation_only(store):
    """Entity with only observations (no slots) in scope is visible."""
    eid = store.resolve_entity("Dashboard", "project")
    store.add_observation(eid, "Web app for agents", scope="dashboard")
    visible = store.list_entities_in_scopes(["dashboard"])
    names = {e["name"] for e in visible}
    assert "Dashboard" in names


def test_list_entities_in_scopes_mixed(store):
    """Both slot-only and observation-only entities appear."""
    e1 = store.resolve_entity("Alice", "person")
    store.set_slot(e1, "role", "dev", scope="acme")
    e2 = store.resolve_entity("Acme Project", "project")
    store.add_observation(e2, "Important fact", scope="acme")
    visible = store.list_entities_in_scopes(["acme"])
    names = {e["name"] for e in visible}
    assert names == {"Alice", "Acme Project"}


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
    eid = store.resolve_entity("Jordan", "person")
    store.add_observation(eid, "observation about databases")
    results = store.search("database")
    assert any(r["name"] == "Jordan" for r in results)

def test_search_multiword(store):
    e1 = store.resolve_entity("Alice", "person")
    store.add_observation(e1, "works at Google")
    e2 = store.resolve_entity("Bob", "person")
    store.add_observation(e2, "photography expert")
    results = store.search("Google photography")
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


# --- rename_scope ---

def test_rename_scope_slots(store):
    """rename_scope migrates current slots."""
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="old-name")
    store.set_slot(eid, "email", "a@test.com", scope="old-name")
    counts = store.rename_scope("old-name", "new-name")
    assert counts["slots"] == 2
    assert store.get_slot(eid, "role", scope_chain=["new-name"]) == "dev"
    assert store.get_slot(eid, "role", scope_chain=["old-name"]) is None


def test_rename_scope_observations(store):
    """rename_scope migrates active observations."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "Fact 1", scope="old-name")
    store.add_observation(eid, "Fact 2", scope="old-name")
    counts = store.rename_scope("old-name", "new-name")
    assert counts["observations"] == 2
    # Verify via entity scopes
    scopes = store.get_entity_scopes(eid)
    assert "new-name" in scopes
    assert "old-name" not in scopes


def test_rename_scope_relations(store):
    """rename_scope migrates active relations."""
    e1 = store.resolve_entity("Alice", "person")
    e2 = store.resolve_entity("Acme", "client")
    store.add_relation(e1, e2, "works_at", scope="old-name")
    counts = store.rename_scope("old-name", "new-name")
    assert counts["relations"] == 1


def test_rename_scope_no_match(store):
    """rename_scope with non-existent scope returns zero counts."""
    counts = store.rename_scope("nonexistent", "new-name")
    assert counts == {"slots": 0, "observations": 0, "relations": 0}


def test_rename_scope_skips_archived(store):
    """rename_scope does not touch archived slots/observations."""
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="old-name")
    store.archive_slot(eid, "role", scope="old-name")
    oid = store.add_observation(eid, "Archived fact", scope="old-name")
    store.archive_observation(oid)
    counts = store.rename_scope("old-name", "new-name")
    assert counts["slots"] == 0
    assert counts["observations"] == 0


# --- Integrity checks ---

def test_find_orphaned_scopes(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="known")
    store.set_slot(eid, "hobby", "chess", scope="orphan")
    store.add_observation(eid, "Note", scope="obs-orphan")
    orphaned = store.find_orphaned_scopes({"global", "known"})
    assert "orphan" in orphaned
    assert "obs-orphan" in orphaned
    assert "known" not in orphaned

def test_find_orphaned_scopes_none(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="global")
    assert store.find_orphaned_scopes({"global"}) == []

def test_find_duplicate_slots(store):
    eid = store.resolve_entity("Alice", "person")
    # Simulate duplicate: insert two active slots with same key+scope
    store._conn.execute(
        "INSERT INTO slots (entity_id, key, value, scope, valid_from) "
        "VALUES (?, 'role', 'dev', 'global', '2025-01-01T00:00:00')",
        (eid,))
    store._conn.execute(
        "INSERT INTO slots (entity_id, key, value, scope, valid_from) "
        "VALUES (?, 'role', 'manager', 'global', '2025-01-02T00:00:00')",
        (eid,))
    store._conn.commit()
    dupes = store.find_duplicate_slots()
    assert len(dupes) == 1
    assert dupes[0]["key"] == "role"
    assert dupes[0]["count"] == 2

def test_find_duplicate_slots_entity_type_filter(store):
    e1 = store.resolve_entity("Alice", "person")
    e2 = store.resolve_entity("draft-1", "draft")
    # Create duplicates for both
    for eid in (e1, e2):
        store._conn.execute(
            "INSERT INTO slots (entity_id, key, value, scope, valid_from) "
            "VALUES (?, 'status', 'a', 'global', '2025-01-01')", (eid,))
        store._conn.execute(
            "INSERT INTO slots (entity_id, key, value, scope, valid_from) "
            "VALUES (?, 'status', 'b', 'global', '2025-01-02')", (eid,))
    store._conn.commit()
    dupes = store.find_duplicate_slots(entity_type="draft")
    assert len(dupes) == 1
    assert dupes[0]["entity_name"] == "draft-1"

def test_find_thin_entities(store):
    e1 = store.resolve_entity("Alice", "person")
    # Alice has 0 slots, 0 observations → thin
    e2 = store.resolve_entity("Bob", "person")
    store.set_slot(e2, "role", "dev")
    store.set_slot(e2, "email", "bob@test.com")
    # Bob has 2 slots → not thin
    thin = store.find_thin_entities()
    names = [t["name"] for t in thin]
    assert "Alice" in names
    assert "Bob" not in names

def test_find_thin_entities_excludes_types(store):
    store.resolve_entity("Alice", "person")
    store.resolve_entity("topic-1", "topic")
    thin = store.find_thin_entities(exclude_types={"topic"})
    names = [t["name"] for t in thin]
    assert "Alice" in names
    assert "topic-1" not in names

def test_check_integrity(store):
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "role", "dev", scope="orphan-scope")
    result = store.check_integrity(valid_scopes={"global"})
    assert "orphan-scope" in result["orphaned_scopes"]
    assert isinstance(result["duplicate_slots"], list)
    assert isinstance(result["thin_entities"], list)

def test_check_integrity_no_valid_scopes(store):
    """check_integrity skips orphan check when valid_scopes is None."""
    result = store.check_integrity()
    assert result["orphaned_scopes"] == []


# --- FTS5 Full-Text Search ---

def test_fts_available(store):
    """FTS5 should be available on Python 3.9+."""
    assert store.has_fts is True


def test_fts_search_exact_name(store):
    """FTS search finds entity by exact name."""
    store.resolve_entity("Alice", "person")
    results = store.search("Alice")
    assert any(r["name"] == "Alice" for r in results)


def test_fts_search_partial_name(store):
    """FTS search finds entity by word prefix via Porter stemmer."""
    store.resolve_entity("Engineering", "department")
    # Porter stemmer: "Engineer" stems similarly to "Engineering"
    results = store.search("Engineer")
    assert any(r["name"] == "Engineering" for r in results)


def test_fts_search_stemming_running_finds_run(store):
    """Porter stemmer handles word forms: 'running' matches 'run'."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "She enjoys running every morning")
    results = store.search("run")
    assert any(r["name"] == "Alice" for r in results)


def test_fts_search_stemming_databases_finds_database(store):
    """Porter stemmer: 'databases' matches 'database' query."""
    eid = store.resolve_entity("Jordan", "person")
    store.add_observation(eid, "Expert in databases and systems")
    results = store.search("database")
    assert any(r["name"] == "Jordan" for r in results)


def test_fts_search_stemming_configured_finds_configure(store):
    """Porter stemmer: 'configured' matches 'configure'."""
    eid = store.resolve_entity("Server", "system")
    store.add_observation(eid, "Server configured for production")
    results = store.search("configure")
    assert any(r["name"] == "Server" for r in results)


def test_fts_search_multiword_or(store):
    """Multi-word FTS query uses OR — matches any word."""
    e1 = store.resolve_entity("Alice", "person")
    store.add_observation(e1, "Works at Google")
    e2 = store.resolve_entity("Bob", "person")
    store.add_observation(e2, "Photography expert")
    results = store.search("Google photography")
    names = {r["name"] for r in results}
    assert "Alice" in names
    assert "Bob" in names


def test_fts_search_observation_text(store):
    """FTS search finds entity via observation content."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "CEO of Acme Corp")
    results = store.search("CEO")
    assert any(r["name"] == "Alice" for r in results)


def test_fts_search_slot_value(store):
    """FTS search still finds entities by slot values (LIKE fallback)."""
    eid = store.resolve_entity("Alice", "person")
    store.set_slot(eid, "email", "alice@example.com")
    results = store.search("alice@example")
    assert any(r["name"] == "Alice" for r in results)


def test_fts_search_archived_observations_excluded(store):
    """FTS search does not return results from archived observations."""
    eid = store.resolve_entity("Alice", "person")
    oid = store.add_observation(eid, "Unique secret fact XYZ123")
    store.archive_observation(oid)
    results = store.search("XYZ123")
    assert not any(r["name"] == "Alice" for r in results)


def test_fts_search_no_results(store):
    """FTS search returns empty list for non-matching query."""
    store.resolve_entity("Alice", "person")
    results = store.search("zzz_nonexistent_zzz")
    assert results == []


def test_fts_search_special_characters(store):
    """FTS search handles special characters without crashing."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "Email: alice@example.com")
    # Should not crash on special FTS characters
    results = store.search("alice@example.com")
    assert isinstance(results, list)


def test_fts_search_empty_query(store):
    """FTS search handles empty query gracefully."""
    store.resolve_entity("Alice", "person")
    results = store.search("")
    assert isinstance(results, list)


def test_fts_triggers_sync_on_insert(store):
    """FTS index is updated when new entity is inserted."""
    store.resolve_entity("UniqueTestName", "person")
    results = store.search("UniqueTestName")
    assert len(results) == 1
    assert results[0]["name"] == "UniqueTestName"


def test_fts_triggers_sync_on_delete(store):
    """FTS index is updated when entity is deleted."""
    eid = store.resolve_entity("DeleteMe", "person")
    results = store.search("DeleteMe")
    assert len(results) == 1
    store.delete_entity(eid)
    results = store.search("DeleteMe")
    assert len(results) == 0


def test_fts_triggers_sync_observation_insert(store):
    """FTS index for observations is updated on insert."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "UniqueObservationText999")
    results = store.search("UniqueObservationText999")
    assert any(r["name"] == "Alice" for r in results)


def test_fts_search_limit(store):
    """FTS search respects the limit parameter."""
    for i in range(20):
        store.resolve_entity(f"TestEntity{i}", "item")
    results = store.search("TestEntity", limit=5)
    assert len(results) <= 5


def test_rebuild_fts(tmp_path):
    """rebuild_fts repopulates FTS indexes from existing data."""
    # Create a store and add data WITHOUT FTS (simulate pre-FTS database)
    db_file = tmp_path / "rebuild.db"
    s = MemoryStore(db_file)
    eid = s.resolve_entity("RebuildTest", "person")
    s.add_observation(eid, "Important fact about rebuilding")
    # Drop FTS tables and triggers to simulate a pre-FTS database
    s._conn.executescript("""
        DROP TABLE IF EXISTS entities_fts;
        DROP TABLE IF EXISTS observations_fts;
        DROP TRIGGER IF EXISTS entities_ai;
        DROP TRIGGER IF EXISTS entities_ad;
        DROP TRIGGER IF EXISTS entities_au;
        DROP TRIGGER IF EXISTS observations_ai;
        DROP TRIGGER IF EXISTS observations_ad;
        DROP TRIGGER IF EXISTS observations_au;
    """)
    s.close()
    # Reopen — _init_fts creates empty FTS tables + triggers
    s = MemoryStore(db_file)
    assert s.has_fts
    # FTS is empty (data was inserted before FTS existed), rebuild it
    s.rebuild_fts()
    results = s.search("RebuildTest")
    assert any(r["name"] == "RebuildTest" for r in results)
    results = s.search("rebuilding")
    assert any(r["name"] == "RebuildTest" for r in results)
    s.close()


def test_rebuild_fts_with_existing_data(tmp_path):
    """rebuild_fts works correctly with pre-existing data in tables."""
    db_file = tmp_path / "rebuild_multi.db"
    s = MemoryStore(db_file)
    for name in ["Alice", "Bob", "Charlie"]:
        eid = s.resolve_entity(name, "person")
        s.add_observation(eid, f"{name} works at Acme Corp")
    # Drop and recreate FTS to simulate upgrade
    s._conn.executescript("""
        DROP TABLE IF EXISTS entities_fts;
        DROP TABLE IF EXISTS observations_fts;
        DROP TRIGGER IF EXISTS entities_ai;
        DROP TRIGGER IF EXISTS entities_ad;
        DROP TRIGGER IF EXISTS entities_au;
        DROP TRIGGER IF EXISTS observations_ai;
        DROP TRIGGER IF EXISTS observations_ad;
        DROP TRIGGER IF EXISTS observations_au;
    """)
    s.close()
    s = MemoryStore(db_file)
    s.rebuild_fts()
    for name in ["Alice", "Bob", "Charlie"]:
        results = s.search(name)
        assert any(r["name"] == name for r in results), f"Failed to find {name} after rebuild"
    s.close()


def test_rebuild_fts_not_available(tmp_path):
    """rebuild_fts raises RuntimeError when FTS5 is not available."""
    s = MemoryStore(tmp_path / "nofts.db")
    s._has_fts = False  # Simulate FTS unavailable
    with pytest.raises(RuntimeError, match="FTS5 is not available"):
        s.rebuild_fts()
    s.close()


def test_fts_backward_compat_like_fallback(tmp_path):
    """When _has_fts is False, search falls back to LIKE-based search."""
    s = MemoryStore(tmp_path / "fallback.db")
    s._has_fts = False  # Simulate FTS unavailable
    eid = s.resolve_entity("Alice", "person")
    s.add_observation(eid, "CEO of Acme Corp")
    results = s.search("CEO")
    assert any(r["name"] == "Alice" for r in results)
    results = s.search("Alice")
    assert any(r["name"] == "Alice" for r in results)
    s.close()


def test_fts_case_insensitive(store):
    """FTS search is case-insensitive (unicode61 tokenizer)."""
    store.resolve_entity("Alice", "person")
    results_lower = store.search("alice")
    results_upper = store.search("ALICE")
    assert any(r["name"] == "Alice" for r in results_lower)
    assert any(r["name"] == "Alice" for r in results_upper)
