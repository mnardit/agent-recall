"""Tests for layered context assembly."""
import pytest
from agent_recall.store import MemoryStore
from agent_recall.context import assemble_context


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db")
    yield s
    s.close()


def _seed_person(store, name, scope="global", role=None, clients=None):
    eid = store.resolve_entity(name, "person")
    if role:
        store.set_slot(eid, "role", role, scope=scope)
    if clients:
        store.set_slot(eid, "clients", clients, scope=scope)
    return eid


# --- Basic assembly ---

def test_empty_chain(store):
    assert assemble_context(store, [], tier=2) == ""


def test_tier_zero_returns_empty(store):
    assert assemble_context(store, ["global", "acme"], tier=0) == ""


def test_people_section(store):
    _seed_person(store, "Alice", scope="acme", role="Engineer")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## People" in result
    assert "Alice" in result
    assert "Engineer" in result


def test_people_with_observations(store):
    eid = _seed_person(store, "Bob", scope="acme", role="Manager")
    store.add_observation(eid, "likes coffee", scope="acme")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "likes coffee" in result


def test_observations_filtered_by_scope(store):
    eid = _seed_person(store, "Carol", scope="acme", role="Designer")
    store.add_observation(eid, "visible note", scope="acme")
    store.add_observation(eid, "hidden note", scope="other-scope")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "visible note" in result
    assert "hidden note" not in result


# --- Topics section ---

def test_topics_in_context(store):
    topic_id = store.resolve_entity("my-topic", "topic")
    store.set_slot(topic_id, "status", "open", scope="global")
    store.set_slot(topic_id, "parent_project", "acme", scope="global")
    store.set_slot(topic_id, "origin", "Testing topic display")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## Topics" in result
    assert "my-topic" in result
    assert "open" in result


def test_topics_require_tier_2(store):
    topic_id = store.resolve_entity("my-topic", "topic")
    store.set_slot(topic_id, "status", "open", scope="global")
    store.set_slot(topic_id, "parent_project", "acme", scope="global")
    result = assemble_context(store, ["global", "acme"], tier=1)
    assert "## Topics" not in result


# --- Client/agency/project entities ---

def test_clients_section(store):
    cid = store.resolve_entity("Acme Corp", "client")
    store.set_slot(cid, "status", "active", scope="acme")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## Clients" in result
    assert "Acme Corp" in result


# --- Recent logs ---

def test_logs_section(store):
    eid = store.resolve_entity("Acme Corp", "client")
    store.set_slot(eid, "status", "active", scope="acme")
    store.add_log(eid, "Started project")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## Recent Log" in result
    assert "Started project" in result


# --- Vault tasks ---

def test_vault_tasks(store, tmp_path):
    projects_dir = tmp_path / "projects"
    acme_dir = projects_dir / "acme"
    acme_dir.mkdir(parents=True)
    (acme_dir / "Project.md").write_text(
        "# Project\n\n## Tasks\n- [ ] Fix bug\n- [ ] Add feature\n\n## Done\n"
    )
    result = assemble_context(
        store, ["global", "acme"], tier=2,
        vault_projects_dir=projects_dir, task_header="## Tasks"
    )
    assert "## Current Tasks" in result
    assert "Fix bug" in result
    assert "Add feature" in result


def test_vault_tasks_custom_header(store, tmp_path):
    projects_dir = tmp_path / "projects"
    acme_dir = projects_dir / "acme"
    acme_dir.mkdir(parents=True)
    (acme_dir / "Project.md").write_text(
        "# Project\n\n## Aufgaben\n- [ ] Task one\n\n## Done\n"
    )
    result = assemble_context(
        store, ["global", "acme"], tier=2,
        vault_projects_dir=projects_dir, task_header="## Aufgaben"
    )
    assert "Task one" in result


def test_no_vault_dir(store):
    """No vault_projects_dir = no tasks section, but no crash."""
    _seed_person(store, "Alice", scope="acme", role="Engineer")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## Current Tasks" not in result


# --- Budget truncation ---

def test_budget_truncation(store):
    for i in range(50):
        _seed_person(store, f"Person{i:03d}", scope="acme",
                     role=f"Role {i}" * 10)
    result = assemble_context(store, ["global", "acme"], tier=2, budget=500)
    assert len(result) <= 500
    assert "omitted" in result


# --- Leaf-scope filtering ---

def test_leaf_scope_filter_excludes_unrelated(store):
    """With 3+ chain elements, people without leaf-scope data are excluded."""
    # Person only in parent scope, not in leaf
    _seed_person(store, "ParentOnly", scope="acme", role="Manager")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    # ParentOnly has no slots in proj-a and no clients value
    assert "ParentOnly" not in result


def test_leaf_scope_filter_includes_with_clients(store):
    """Person with matching clients value passes leaf filter."""
    eid = _seed_person(store, "WithClients", scope="acme", role="Manager",
                       clients="acme, proj-a")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "WithClients" in result


def test_leaf_scope_filter_includes_with_leaf_slots(store):
    """Person with slots in leaf scope passes filter."""
    eid = _seed_person(store, "LeafPerson", scope="proj-a", role="Dev")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "LeafPerson" in result


# --- Project Context (observation-only entities) ---

def test_project_context_observation_only(store):
    """Entity with observations but no slots appears in Project Context."""
    eid = store.resolve_entity("My Dashboard", "project")
    store.add_observation(eid, "Feature X deployed", scope="acme")
    result = assemble_context(store, ["global", "acme"], tier=1)
    assert "## Project Context" in result
    assert "My Dashboard" in result
    assert "Feature X deployed" in result


def test_project_context_skips_persons(store):
    """Person entities don't appear in Project Context."""
    eid = store.resolve_entity("Alice", "person")
    store.add_observation(eid, "Some note", scope="acme")
    result = assemble_context(store, ["global", "acme"], tier=1)
    assert "## Project Context" not in result


# --- Deep chain: primary/secondary people ---

def test_deep_chain_primary_people(store):
    """Person with leaf scope in clients is shown with full detail."""
    eid = _seed_person(store, "Alice", scope="acme", role="Developer",
                       clients="acme, proj-a")
    store.add_observation(eid, "Works on frontend", scope="acme")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "**Alice**" in result
    assert "Works on frontend" in result


def test_deep_chain_secondary_people(store):
    """Person with parent-only in clients appears as brief team member."""
    _seed_person(store, "Bob", scope="acme", role="Designer",
                 clients="acme")
    store.add_observation(store.find_entity("Bob"), "Loves CSS", scope="acme")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "Bob" in result
    assert "Other team members" in result
    assert "Designer" in result
    # Secondary people don't get observations
    assert "Loves CSS" not in result


def test_deep_chain_excludes_unrelated(store):
    """Person with neither leaf nor parent in clients is excluded."""
    _seed_person(store, "Carol", scope="other", role="PM",
                 clients="other-org, other-proj")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "Carol" not in result


def test_deep_chain_clients_tokenized(store):
    """Substring matching doesn't cause false positives."""
    # "pro" should not match "proj-a" via substring
    _seed_person(store, "Dan", scope="acme", role="Intern",
                 clients="acme, pro")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    # Dan has "acme" (parent) but not "proj-a" (leaf) → secondary
    assert "Other team members" in result
    assert "Dan" in result
    # Verify "pro" didn't match "proj-a" as primary
    assert "**Dan**" not in result


# --- Deep chain: log filtering ---

def test_deep_chain_logs_leaf_only(store):
    """Logs from sibling-scope entities are excluded for deep chains."""
    # Entity with data only in sibling scope
    sibling_eid = store.resolve_entity("Sibling Project", "project")
    store.set_slot(sibling_eid, "name", "Sibling", scope="sibling-proj")
    store.add_log(sibling_eid, "Sibling event")
    # Entity with data in leaf scope
    leaf_eid = store.resolve_entity("Leaf Project", "project")
    store.set_slot(leaf_eid, "name", "Leaf", scope="proj-a")
    store.add_log(leaf_eid, "Leaf event")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "Leaf event" in result
    assert "Sibling event" not in result


def test_short_chain_logs_unchanged(store):
    """For 2-level chain, all visible entity logs are shown."""
    eid = store.resolve_entity("Project X", "project")
    store.set_slot(eid, "name", "X", scope="acme")
    store.add_log(eid, "Project X event")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "Project X event" in result


# --- Deep chain: parent context ---

def test_parent_context_section(store):
    """Parent entity info appears for 3+ chain."""
    parent_eid = store.resolve_entity("acme", "agency")
    store.set_slot(parent_eid, "name", "Acme Corp", scope="global")
    store.add_observation(parent_eid, "Digital marketing agency", scope="global")
    result = assemble_context(store, ["global", "acme", "proj-a"], tier=2)
    assert "## Parent Context" in result
    assert "Acme Corp" in result
    assert "Digital marketing agency" in result


def test_no_parent_context_short_chain(store):
    """No parent context for 2-level chain."""
    parent_eid = store.resolve_entity("acme", "agency")
    store.set_slot(parent_eid, "name", "Acme Corp", scope="global")
    result = assemble_context(store, ["global", "acme"], tier=2)
    assert "## Parent Context" not in result
